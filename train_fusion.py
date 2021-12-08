import torch
import argparse
import datetime
import numpy as np
import random

from tqdm import tqdm
import math

from utils.setup import *
from utils.loading import *
from utils.loss import *

from modules.pipeline import Pipeline


def arg_parser():

    parser = argparse.ArgumentParser()

    parser.add_argument("--config")

    args = parser.parse_args()
    return vars(args)


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group["lr"]


def train_fusion(args):

    config = load_config_from_yaml(args["config"])

    # assert not (config.LOSS.gt_loss and config.FILTERING_MODEL.w_features), "You can only use gt loss when not using features"

    config.TIMESTAMP = datetime.datetime.now().strftime("%y%m%d-%H%M%S")

    # set seed for reproducibility
    if config.SETTINGS.seed:
        random.seed(config.SETTINGS.seed)
        np.random.seed(config.SETTINGS.seed)
        torch.manual_seed(config.SETTINGS.seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    # get workspace
    workspace = get_workspace(config)

    # save config before training
    workspace.save_config(config)

    if config.SETTINGS.gpu:
        device = torch.device("cuda:0")
    else:
        device = torch.device("cpu")
    config.FUSION_MODEL.device = device

    # torch.autograd.set_detect_anomaly(True) # slows down training. Use for debugging

    # get datasets
    # get train dataset
    train_data_config = get_data_config(config, mode="train")
    train_dataset = get_data(config.DATA.dataset, train_data_config)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, config.TRAINING.train_batch_size, config.TRAINING.train_shuffle
    )

    # get val dataset
    val_data_config = get_data_config(config, mode="val")
    val_dataset = get_data(config.DATA.dataset, val_data_config)
    val_loader = torch.utils.data.DataLoader(
        val_dataset, config.TRAINING.val_batch_size, config.TRAINING.val_shuffle
    )

    # specify number of features
    if config.FEATURE_MODEL.learned_features:
        if not config.LOSS.alpha_2d_supervision:
            assert config.FEATURE_MODEL.append_pixel_conf == False
        config.FEATURE_MODEL.n_features = (
            config.FEATURE_MODEL.append_pixel_conf
            + config.FEATURE_MODEL.n_features
            + config.FEATURE_MODEL.append_depth
        )
    else:
        config.FEATURE_MODEL.n_features = (
            config.FEATURE_MODEL.append_pixel_conf
            + config.FEATURE_MODEL.append_depth
            + 3 * config.FEATURE_MODEL.w_rgb
        )  # 1 for label encoding of noise in gaussian threshold data

    # get database
    # get train database
    train_database = get_database(train_dataset, config, mode="train")
    val_database = get_database(val_dataset, config, mode="val")

    # setup pipeline
    pipeline = Pipeline(config)
    pipeline = pipeline.to(device)  # put the networks on the gpu

    for sensor in config.DATA.input:
        if config.FUSION_MODEL.use_fusion_net:
            print(
                "Fusion Net ",
                sensor,
                ": ",
                count_parameters(pipeline.fuse_pipeline._fusion_network[sensor]),
            )
        print(
            "Feature Net ",
            sensor,
            ": ",
            count_parameters(pipeline.fuse_pipeline._feature_network[sensor]),
        )

    if pipeline.filter_pipeline is not None:
        print(
            "Filtering Net: ",
            count_parameters(pipeline.filter_pipeline._filtering_network),
        )
    print("Fusion and Filtering: ", count_parameters(pipeline))

    # optimization
    criterion = Fusion_TranslationLoss(config)

    # load pretrained routing model into parameters
    if config.ROUTING.do:
        if config.DATA.fusion_strategy == "routingNet":
            routing_checkpoint = torch.load(config.TESTING.routing_model_path)
            # print(routing_checkpoint)
            # load_model(config.TESTING.routing_model_path, pipeline._routing_network)
            # Keep line below until I see that the new loading function works.
            pipeline.fuse_pipeline._routing_network.load_state_dict(
                routing_checkpoint["pipeline_state_dict"]
            )
        elif config.DATA.fusion_strategy == "fusionNet":
            for sensor_ in config.DATA.input:
                checkpoint = torch.load(
                    eval("config.TRAINING.routing_" + sensor_ + "_model_path")
                )
                pipeline.fuse_pipeline._routing_network[sensor_].load_state_dict(
                    checkpoint["pipeline_state_dict"]
                )

    if config.TESTING.pretrain_filtering_net:
        load_pipeline(
            config.TESTING.fusion_model_path, pipeline
        )  # this is the filtering loading

    if config.TRAINING.pretraining and config.FUSION_MODEL.use_fusion_net:
        for sensor in config.DATA.input:
            if not config.ROUTING.do:
                if sensor == "tof" or sensor == "stereo":
                    load_net_old(
                        eval(
                            "config.TRAINING.pretraining_fusion_"
                            + sensor
                            + "_model_path"
                        ),
                        pipeline.fuse_pipeline._fusion_network[sensor],
                        sensor,
                    )
                else:
                    load_net(
                        eval(
                            "config.TRAINING.pretraining_fusion_"
                            + sensor
                            + "_model_path"
                        ),
                        pipeline.fuse_pipeline._fusion_network[sensor],
                        sensor,
                    )
            else:
                load_net(
                    eval(
                        "config.TRAINING.pretraining_fusionrouting_"
                        + sensor
                        + "_model_path"
                    ),
                    pipeline.fuse_pipeline._fusion_network[sensor],
                    sensor,
                )
                # load_net(eval('config.TRAINING.pretraining_fusion_' + sensor +  '_model_path'), pipeline.fuse_pipeline._fusion_network[sensor], sensor)
            # loading gt depth model fusion net
            # load_net('/cluster/work/cvl/esandstroem/src/late_fusion_3dconvnet/workspace/fusion/210507-093251/model/best.pth.tar', pipeline.fuse_pipeline._fusion_network[sensor], 'left_depth_gt_2')

    if (
        config.FILTERING_MODEL.features_to_sdf_enc
        or config.FILTERING_MODEL.features_to_weight_head
    ):
        feature_params = []
        for sensor in config.DATA.input:
            feature_params += list(
                pipeline.fuse_pipeline._feature_network[sensor].parameters()
            )

        # optimizer_feature = torch.optim.RMSprop(feature_params,
        #                                         config.OPTIMIZATION.lr_fusion,
        #                                         config.OPTIMIZATION.rho,
        #                                         config.OPTIMIZATION.eps,
        #                                         momentum=config.OPTIMIZATION.momentum,
        #                                         weight_decay=config.OPTIMIZATION.weight_decay)

        optimizer_feature = torch.optim.Adam(
            feature_params,
            config.OPTIMIZATION.lr_fusion,
            betas=(0.9, 0.999),
            eps=1e-08,
            weight_decay=0,
        )

        scheduler_feature = torch.optim.lr_scheduler.StepLR(
            optimizer=optimizer_feature,
            step_size=config.OPTIMIZATION.scheduler.step_size_fusion,
            gamma=config.OPTIMIZATION.scheduler.gamma_fusion,
        )

    if not config.FILTERING_MODEL.fixed and pipeline.filter_pipeline is not None:
        # optimizer_filt = torch.optim.RMSprop(pipeline.filter_pipeline._filtering_network.parameters(),
        #                                     config.OPTIMIZATION.lr_filtering,
        #                                     config.OPTIMIZATION.rho,
        #                                     config.OPTIMIZATION.eps,
        #                                     momentum=config.OPTIMIZATION.momentum,
        #                                     weight_decay=config.OPTIMIZATION.weight_decay)

        optimizer_filt = torch.optim.Adam(
            pipeline.filter_pipeline._filtering_network.parameters(),
            config.OPTIMIZATION.lr_filtering,
            betas=(0.9, 0.999),
            eps=1e-08,
            weight_decay=0,
        )

        scheduler_filt = torch.optim.lr_scheduler.StepLR(
            optimizer=optimizer_filt,
            step_size=config.OPTIMIZATION.scheduler.step_size_filtering,
            gamma=config.OPTIMIZATION.scheduler.gamma_filtering,
        )

    if not config.FUSION_MODEL.fixed and config.FUSION_MODEL.use_fusion_net:
        fusion_params = []
        for sensor in config.DATA.input:
            fusion_params += list(
                pipeline.fuse_pipeline._fusion_network[sensor].parameters()
            )
        optimizer_fusion = torch.optim.RMSprop(
            fusion_params,
            config.OPTIMIZATION.lr_fusion,
            config.OPTIMIZATION.rho,
            config.OPTIMIZATION.eps,
            momentum=config.OPTIMIZATION.momentum,
            weight_decay=config.OPTIMIZATION.weight_decay,
        )

        scheduler_fusion = torch.optim.lr_scheduler.StepLR(
            optimizer=optimizer_fusion,
            step_size=config.OPTIMIZATION.scheduler.step_size_fusion,
            gamma=config.OPTIMIZATION.scheduler.gamma_fusion,
        )

    # define some parameters
    n_batches = float(len(train_dataset) / config.TRAINING.train_batch_size)

    # evaluation metrics
    best_iou_filt = 0.0  # best filtered
    is_best_filt = False

    best_iou = dict()
    is_best = dict()
    for sensor in config.DATA.input:
        best_iou[sensor] = 0.0  # USE THIS FOR REAL TRAINING
        # best_iou[sensor] = 10000 # USE THIS FOR FEATURE NETWORK TRAINING
        is_best[sensor] = False

    # copy sensor list so that we can shuffle the sensors but still have the same
    # sensor at index 0 and index 1 as originally in the config file input
    sensors = config.DATA.input.copy()

    for epoch in range(0, config.TRAINING.n_epochs):

        workspace.log(
            "Training epoch {}/{}".format(epoch, config.TRAINING.n_epochs), mode="train"
        )

        pipeline.train()  # need to change! Check so that gradients can pass!

        if config.ROUTING.do:
            pipeline.fuse_pipeline._routing_network.eval()
        if config.FUSION_MODEL.fixed and config.FUSION_MODEL.use_fusion_net:
            pipeline.fuse_pipeline._fusion_network.eval()
        if config.FILTERING_MODEL.fixed and pipeline.filter_pipeline is not None:
            pipeline.filter_pipeline._filtering_network.eval()

        # resetting databases before each epoch starts
        train_database.reset()
        val_database.reset()

        # I want to handle the plotting of training data in a more elegant way - probably best to include this
        # functionality in the workspace so that it can do the writing of the appropriate properties
        divide = 0
        train_loss = 0
        grad_norm_alpha_net = 0
        grad_norm_feature = dict()
        grad_norm_outlier_net = dict()
        val_norm = 0
        l1_interm = 0
        l1_grid = 0
        l1_grid_dict = dict()
        l_occ_dict = dict()
        l_alpha_2d = dict()
        for sensor_ in config.DATA.input:
            l1_grid_dict[sensor_] = 0
            l_occ_dict[sensor_] = 0
            grad_norm_feature[sensor_] = 0
            grad_norm_outlier_net[sensor_] = 0
            l_alpha_2d[sensor_] = 0

        l1_gt_grid = 0
        l_feat = 0

        l_occ = 0  # single sensor training

        for i, batch in tqdm(enumerate(train_loader), total=len(train_dataset)):
            # reset the database for every new trajectory (if using hybrid loading strategy)
            # if batch['frame_id'][0].split('/')[-1] == '0' and config.DATA.data_load_strategy == 'hybrid':
            #     workspace.log('Starting new trajectory {} at step {}'.format(batch['frame_id'][0][:-2], i), mode='train')
            #     workspace.log('Resetting grid for scene {} at step {}'.format(batch['frame_id'][0].split('/')[0], i),
            #                       mode='train')
            #     train_database.reset(batch['frame_id'][0].split('/')[0])

            if config.TRAINING.reset_strategy:
                if np.random.random_sample() <= config.TRAINING.reset_prob:
                    workspace.log(
                        "Resetting randomly trajectory {} at step {}".format(
                            batch["frame_id"][0][:-2], i
                        ),
                        mode="train",
                    )
                    workspace.log(
                        "Resetting grid for scene {} at step {}".format(
                            batch["frame_id"][0].split("/")[0], i
                        ),
                        mode="train",
                    )
                    train_database.reset(batch["frame_id"][0].split("/")[0])

            # take care of the fusion strategy here i.e. loop through the 3 integrations randomly by adding the "mask" and depth
            # as keys in the batch. But I also need knowledge of sensor label for the routing network. I create the 'routing_net'
            # and 'depth' keys and pass that to the fuse_training function in three steps. Also pass the routing threshold as a
            # key.
            if config.DATA.collaborative_reconstruction:
                if (
                    math.ceil(
                        int(batch["frame_id"][0].split("/")[-1])
                        / config.DATA.frames_per_chunk
                    )
                    % 2
                    == 0
                ):
                    sensor = config.DATA.input[0]
                else:
                    sensor = config.DATA.input[1]

                batch["depth"] = batch[sensor + "_depth"]
                # batch['confidence_threshold'] = eval('config.ROUTING.threshold_' + sensor) # not relevant to use anymore
                batch["mask"] = batch[sensor + "_mask"]
                if config.FILTERING_MODEL.model == "routedfusion":
                    batch["sensor"] = config.DATA.input[0]
                else:
                    batch["sensor"] = sensor

                batch["routingNet"] = sensor  # used to be able to train routedfusion
                batch["fusionNet"] = sensor  # used to be able to train routedfusion
                output = pipeline(batch, train_database, epoch, device)

                # optimization
                if (
                    output is None
                ):  # output is None when no valid indices were found for the filtering net within the random
                    # bbox within the bounding volume of the integrated indices
                    print("output None from pipeline")
                    # break
                    continue

                if output == "save_and_exit":
                    print("Found alpha nan. Save and exit")
                    workspace.save_model_state(
                        {"pipeline_state_dict": pipeline.state_dict(), "epoch": epoch},
                        is_best=is_best,
                        is_best_filt=is_best_filt,
                    )
                    return

                output = criterion(output)

                # loss = criterion(output['tsdf_filtered_grid'], output['tsdf_target_grid'])
                # if loss.grad_fn: # this is needed because when the mono mask filters out all pixels, this results in a failure
                # print('bef backward: ', torch.cuda.memory_allocated(device))
                if output["loss"] is not None:
                    divide += 1
                    train_loss += output[
                        "loss"
                    ].item()  # note that this loss is a moving average over the training window of log_freq steps
                if output["l1_interm"] is not None:
                    l1_interm += output[
                        "l1_interm"
                    ].item()  # note that this loss is a moving average over the training window of log_freq steps
                if output["l1_grid"] is not None:
                    l1_grid += output["l1_grid"].item()
                if output["l1_gt_grid"] is not None:
                    l1_gt_grid += output["l1_gt_grid"].item()

                if len(config.DATA.input) > 1:
                    for sensor_ in config.DATA.input:
                        if output["l1_grid_" + sensor_] is not None:
                            l1_grid_dict[sensor_] += output["l1_grid_" + sensor_].item()
                    if output["l_alpha_2d"] is not None:
                        l_alpha_2d[batch["sensor"]] += output["l_alpha_2d"]
                if (
                    config.FILTERING_MODEL.model == "mlp"
                    and config.FILTERING_MODEL.setting == "translate"
                    and config.FILTERING_MODEL.MLP_MODEL.occ_head
                ):
                    for sensor_ in config.DATA.input:
                        if output["l_occ_" + sensor_] is not None:
                            l_occ_dict[sensor_] += output["l_occ_" + sensor_].item()
                    if output["l_occ"] is not None:
                        l_occ += output["l_occ"].item()

                if output["loss"] is not None:
                    # print('4m: ', torch.cuda.max_memory_allocated(device=device))
                    # print('4: ', torch.cuda.memory_allocated(device=device))
                    output["loss"].backward()
                    # print('5m: ', torch.cuda.max_memory_allocated(device=device))
                    # print('5: ', torch.cuda.memory_allocated(device=device))
                # break
            else:
                # fusion pipeline
                # randomly integrate the selected sensors
                random.shuffle(sensors)

                for sensor in sensors:
                    batch["depth"] = batch[sensor + "_depth"]
                    # batch['confidence_threshold'] = eval('config.ROUTING.threshold_' + sensor) # not relevant to use anymore
                    batch["mask"] = batch[sensor + "_mask"]

                    if config.FILTERING_MODEL.model == "routedfusion":
                        batch["sensor"] = config.DATA.input[0]
                    else:
                        batch["sensor"] = sensor
                    batch[
                        "routingNet"
                    ] = sensor  # used to be able to train routedfusion
                    batch["fusionNet"] = sensor  # used to be able to train routedfusion
                    output = pipeline(batch, train_database, epoch, device)

                    # optimization
                    if (
                        output is None
                    ):  # output is None when no valid indices were found for the filtering net within the random
                        # bbox within the bounding volume of the integrated indices
                        print("output None from pipeline")
                        # break
                        continue

                    if output == "save_and_exit":
                        print("Found alpha nan. Save and exit")
                        workspace.save_model_state(
                            {
                                "pipeline_state_dict": pipeline.state_dict(),
                                "epoch": epoch,
                            },
                            is_best=is_best,
                            is_best_filt=is_best_filt,
                        )
                        return

                    output = criterion(output)

                    # loss = criterion(output['tsdf_filtered_grid'], output['tsdf_target_grid'])
                    # if loss.grad_fn: # this is needed because when the mono mask filters out all pixels, this results in a failure
                    # print('bef backward: ', torch.cuda.memory_allocated(device))
                    if output["loss"] is not None:
                        divide += 1
                        train_loss += output[
                            "loss"
                        ].item()  # note that this loss is a moving average over the training window of log_freq steps
                    if output["l1_interm"] is not None:
                        l1_interm += output[
                            "l1_interm"
                        ].item()  # note that this loss is a moving average over the training window of log_freq steps
                    if output["l1_grid"] is not None:
                        l1_grid += output["l1_grid"].item()
                    if output["l1_gt_grid"] is not None:
                        l1_gt_grid += output["l1_gt_grid"].item()

                    if len(config.DATA.input) > 1:
                        for sensor_ in config.DATA.input:
                            if output["l1_grid_" + sensor_] is not None:
                                l1_grid_dict[sensor_] += output[
                                    "l1_grid_" + sensor_
                                ].item()
                        if output["l_alpha_2d"] is not None:
                            l_alpha_2d[batch["sensor"]] += output["l_alpha_2d"]
                    if (
                        config.FILTERING_MODEL.model == "mlp"
                        and config.FILTERING_MODEL.setting == "translate"
                        and config.FILTERING_MODEL.MLP_MODEL.occ_head
                    ):
                        for sensor_ in config.DATA.input:
                            if output["l_occ_" + sensor_] is not None:
                                l_occ_dict[sensor_] += output["l_occ_" + sensor_].item()
                        if output["l_occ"] is not None:
                            l_occ += output["l_occ"].item()

                    if output["loss"] is not None:
                        # print('4m: ', torch.cuda.max_memory_allocated(device=device))
                        # print('4: ', torch.cuda.memory_allocated(device=device))
                        output["loss"].backward()
                        # print('5m: ', torch.cuda.max_memory_allocated(device=device))
                        # print('5: ', torch.cuda.memory_allocated(device=device))
                    # break

            del batch

            for name, param in pipeline.named_parameters():
                if param.grad is not None:
                    # print(name)
                    # print(param.grad)
                    if (
                        (i + 1) % config.OPTIMIZATION.accumulation_steps == 0
                        or i == n_batches - 1
                    ):
                        if name.startswith("fuse_pipeline._feature"):
                            grad_norm_feature[name.split(".")[2]] += torch.norm(
                                param.grad
                            )
                        elif name.startswith(
                            "filter_pipeline._filtering_network.encoder"
                        ) or name.startswith("filter_pipeline._filtering_network.sdf"):
                            grad_norm_outlier_net[name.split(".")[3]] += torch.norm(
                                param.grad
                            )
                        else:
                            grad_norm_alpha_net += torch.norm(param.grad)
                        # print(torch.norm(param.grad))
                    # optimizer.zero_grad() # REMOVE LATER!
                    val_norm += torch.norm(param)
                    # print('grad norm: ', torch.norm(param.grad))
                    # print('val norm: ' , torch.norm(param))
                # if name.startswith('fuse_pipeline._feature'):

                # if param.isnan().sum() > 0:
                #     print(name)
                #     print(param)
                # print('isnan sum: ', param.isnan().sum())
                # if param.grad is not None:
                #     print(name)
                #     print(param.grad)

                # Note, gradients that have been not None at one time, will never
                # be none again since the zero_Grad option just makes them zero again.
                # In pytorch 1.7.1 there is the option to set the gradients to none again

                # print(name, param.grad)

            if (i + 1) % config.SETTINGS.log_freq == 0:
                print("log!")
                print("i ", i)
                #
                # if config.DATA.fusion_strategy == 'two_fusionNet': # TODO: split plotting into tof and stereo
                #     divide = 2*config.SETTINGS.log_freq
                # else:
                train_loss /= divide
                grad_norm_alpha_net /= divide

                val_norm /= divide
                # print('averaged grad norm: ', grad_norm)
                # print('averaged val norm: ', val_norm)
                l1_interm /= divide
                l1_grid /= divide
                l1_gt_grid /= divide
                for sensor_ in config.DATA.input:
                    l1_grid_dict[sensor_] /= divide
                    l_occ_dict[sensor_] /= divide
                    grad_norm_feature[sensor_] /= divide
                    grad_norm_outlier_net[sensor_] /= divide
                    l_alpha_2d[sensor_] /= divide

                l_occ /= divide

                # save if loss is lower than before / only for feature network training. Evaluate each 957 frames
                # check if current checkpoint is best
                # is_best_filt = False

                # for sensor in config.DATA.input:
                #     if l_alpha_2d[sensor] <= best_iou[sensor]:
                #         is_best[sensor] = True
                #         best_iou[sensor] = l_alpha_2d[sensor]
                #         workspace.log('found new best ' + sensor + ' model with loss {} at epoch {}'.format(best_iou[sensor], epoch),
                #                       mode='val')

                #     else:
                #         is_best[sensor] = False

                # # save checkpoint
                # workspace.save_model_state({'pipeline_state_dict': pipeline.state_dict(),
                #                                 'epoch': epoch},
                #                                is_best_filt=is_best_filt, is_best=is_best)

                # l_occ /= divide
                # l_feat /= divide
                workspace.writer.add_scalar(
                    "Train/loss", train_loss, global_step=i + 1 + epoch * n_batches
                )
                workspace.writer.add_scalar(
                    "Train/grad_norm_alpha_net",
                    grad_norm_alpha_net,
                    global_step=i + 1 + epoch * n_batches,
                )

                workspace.writer.add_scalar(
                    "Train/val_norm", val_norm, global_step=i + 1 + epoch * n_batches
                )
                # workspace.writer.add_scalar('Train/lr_filt', get_lr(optimizer_filt), global_step=i + 1 + epoch*n_batches)
                # workspace.writer.add_scalar('Train/lr_fusion', get_lr(optimizer_fusion), global_step=i + 1 + epoch*n_batches)
                # workspace.writer.add_scalar('Train/loss_coeff', loss_coeff(i), global_step=i + 1 + epoch*n_batches)
                workspace.writer.add_scalar(
                    "Train/l1_interm", l1_interm, global_step=i + 1 + epoch * n_batches
                )
                workspace.writer.add_scalar(
                    "Train/l1_translation",
                    l1_grid,
                    global_step=i + 1 + epoch * n_batches,
                )
                workspace.writer.add_scalar(
                    "Train/l1_gt_translation",
                    l1_gt_grid,
                    global_step=i + 1 + epoch * n_batches,
                )
                for sensor_ in config.DATA.input:
                    workspace.writer.add_scalar(
                        "Train/grad_norm_feature_" + sensor_,
                        grad_norm_feature[sensor_],
                        global_step=i + 1 + epoch * n_batches,
                    )
                    workspace.writer.add_scalar(
                        "Train/grad_norm_outlier_net" + sensor_,
                        grad_norm_outlier_net[sensor_],
                        global_step=i + 1 + epoch * n_batches,
                    )
                    workspace.writer.add_scalar(
                        "Train/l1_" + sensor_,
                        l1_grid_dict[sensor_],
                        global_step=i + 1 + epoch * n_batches,
                    )
                    workspace.writer.add_scalar(
                        "Train/occ_loss_" + sensor_,
                        l_occ_dict[sensor_],
                        global_step=i + 1 + epoch * n_batches,
                    )
                    workspace.writer.add_scalar(
                        "Train/l_alpha_2d_" + sensor_,
                        l_alpha_2d[sensor_],
                        global_step=i + 1 + epoch * n_batches,
                    )

                workspace.writer.add_scalar(
                    "Train/occ_loss", l_occ, global_step=i + 1 + epoch * n_batches
                )
                # workspace.writer.add_scalar('Train/feat_loss', l_feat, global_step=i + 1 + epoch*n_batches)
                divide = 0
                train_loss = 0
                grad_norm_alpha_net = 0
                grad_norm_feature = dict()
                grad_norm_outlier_net = dict()
                val_norm = 0
                l1_interm = 0
                l1_grid = 0
                l1_grid_dict = dict()
                l_occ_dict = dict()
                for sensor_ in config.DATA.input:
                    l1_grid_dict[sensor_] = 0
                    l_occ_dict[sensor_] = 0
                    grad_norm_feature[sensor_] = 0
                    grad_norm_outlier_net[sensor_] = 0
                    l_alpha_2d[sensor_] = 0

                l1_gt_grid = 0
                l_feat = 0
                l_occ = 0  # single sensor training

            if config.TRAINING.gradient_clipping:
                torch.nn.utils.clip_grad_norm_(
                    pipeline.parameters(), max_norm=1.0, norm_type=2
                )

            if (
                i + 1
            ) % config.OPTIMIZATION.accumulation_steps == 0 or i == n_batches - 1:
                if (
                    config.FILTERING_MODEL.features_to_sdf_enc
                    or config.FILTERING_MODEL.features_to_weight_head
                ):
                    optimizer_feature.step()
                    scheduler_feature.step()
                    optimizer_feature.zero_grad(set_to_none=True)

                if not config.FILTERING_MODEL.fixed:
                    # make the gradients belonging to layers with zero-norm gradient none instead of zero to avoid update
                    # of weights - this is a debugging test to see if the system responds appropriately
                    for name, param in pipeline.named_parameters():
                        if param.grad is not None:
                            if name.startswith(
                                "filter_pipeline._filtering_network.weight_decoder"
                            ):
                                if torch.norm(param.grad) == 0:
                                    print("gradient norm is zero for: ", name)
                                    # print(torch.norm(param.grad))
                                    param.grad = None
                                    # print(param.grad)

                    optimizer_filt.step()
                    scheduler_filt.step()
                    optimizer_filt.zero_grad(set_to_none=True)
                if not config.FUSION_MODEL.fixed:
                    optimizer_fusion.step()
                    scheduler_fusion.step()
                    optimizer_fusion.zero_grad(set_to_none=True)

            # if False:
            if (
                (i + 1) % config.SETTINGS.eval_freq == 0
                or i == n_batches - 1
                or (i == 2 and epoch == 0)
            ):  # evaluate after 20 steps wince then we have integrated at least one frame for each scene
                # if epoch % 2 == 0 and i == 0:
                # print(i)
                val_database.reset()
                # zero out all grads
                if (
                    config.FILTERING_MODEL.features_to_sdf_enc
                    or config.FILTERING_MODEL.features_to_weight_head
                ):
                    optimizer_feature.zero_grad(set_to_none=True)
                if not config.FILTERING_MODEL.fixed:
                    optimizer_filt.zero_grad(set_to_none=True)
                if not config.FUSION_MODEL.fixed:
                    optimizer_fusion.zero_grad(set_to_none=True)

                pipeline.eval()

                pipeline.test(
                    val_loader,
                    val_dataset,
                    val_database,
                    config.DATA.input,
                    device,
                )

                # val_database.filter(value=1.) # the more frames you integrate, the higher can the value be
                val_eval, val_eval_fused = val_database.evaluate(
                    mode="val", workspace=workspace
                )

                for sensor in config.DATA.input:
                    workspace.writer.add_scalar(
                        "Val/mse_" + sensor,
                        val_eval[sensor]["mse"],
                        global_step=i + 1 + epoch * n_batches,
                    )
                    workspace.writer.add_scalar(
                        "Val/acc_" + sensor,
                        val_eval[sensor]["acc"],
                        global_step=i + 1 + epoch * n_batches,
                    )
                    workspace.writer.add_scalar(
                        "Val/iou_" + sensor,
                        val_eval[sensor]["iou"],
                        global_step=i + 1 + epoch * n_batches,
                    )
                    workspace.writer.add_scalar(
                        "Val/mad_" + sensor,
                        val_eval[sensor]["mad"],
                        global_step=i + 1 + epoch * n_batches,
                    )

                workspace.writer.add_scalar(
                    "Val/mse_fused",
                    val_eval_fused["mse"],
                    global_step=i + 1 + epoch * n_batches,
                )
                workspace.writer.add_scalar(
                    "Val/acc_fused",
                    val_eval_fused["acc"],
                    global_step=i + 1 + epoch * n_batches,
                )
                workspace.writer.add_scalar(
                    "Val/iou_fused",
                    val_eval_fused["iou"],
                    global_step=i + 1 + epoch * n_batches,
                )
                workspace.writer.add_scalar(
                    "Val/mad_fused",
                    val_eval_fused["mad"],
                    global_step=i + 1 + epoch * n_batches,
                )

                # check if current checkpoint is best
                if val_eval_fused["iou"] >= best_iou_filt:
                    is_best_filt = True
                    best_iou_filt = val_eval_fused["iou"]
                    workspace.log(
                        "found new best model overall with iou {} at epoch {}".format(
                            best_iou_filt, epoch
                        ),
                        mode="val",
                    )

                else:
                    is_best_filt = False

                for sensor in config.DATA.input:
                    if val_eval[sensor]["iou"] >= best_iou[sensor]:
                        is_best[sensor] = True
                        best_iou[sensor] = val_eval[sensor]["iou"]
                        workspace.log(
                            "found new best "
                            + sensor
                            + " model with iou {} at epoch {}".format(
                                best_iou[sensor], epoch
                            ),
                            mode="val",
                        )

                    else:
                        is_best[sensor] = False

                # save models
                # train_database.save_to_workspace(workspace, mode='latest_train', save_mode=config.SETTINGS.save_mode)
                # val_database.save_to_workspace(workspace, is_best, is_best_tof, is_best_stereo, save_mode=config.SETTINGS.save_mode)

                # save alpha histogram
                workspace.save_alpha_histogram(val_database, config.DATA.input, epoch)

                # save checkpoint
                workspace.save_model_state(
                    {"pipeline_state_dict": pipeline.state_dict(), "epoch": epoch},
                    is_best=is_best,
                    is_best_filt=is_best_filt,
                )

                pipeline.train()
                if config.ROUTING.do:
                    pipeline.fuse_pipeline._routing_network.eval()
                if config.FUSION_MODEL.fixed and config.FUSION_MODEL.use_fusion_net:
                    pipeline.fuse_pipeline._fusion_network.eval()
                if (
                    config.FILTERING_MODEL.fixed
                    and pipeline.filter_pipeline is not None
                ):
                    pipeline.filter_pipeline._filtering_network.eval()

            # if i == 6: # for debugging
            # break


if __name__ == "__main__":

    args = arg_parser()
    train_fusion(args)
