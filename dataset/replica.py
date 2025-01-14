import os
import glob

import sys
import numpy as np

from skimage import io
from skimage.color import rgb2gray
from skimage import filters
from torch.utils.data import Dataset

import torch  # for experiment on asynchronous data

# used when saving images for debugging
# import matplotlib.pyplot as plt

import h5py


class Replica(Dataset):
    def __init__(self, config_data):
        self.root_dir = os.getenv(config_data.root_dir)

        # os.getenv returns none when the input does not exist. When
        # it returns none, we want to train on the work folder
        if not self.root_dir:
            self.root_dir = config_data.root_dir

        self.resolution_stereo = (config_data.resy_stereo, config_data.resx_stereo)

        self.resolution_tof = (config_data.resy_tof, config_data.resx_tof)

        self.resolution = (config_data.resy, config_data.resx)

        self.asynch = config_data.early_fusion_asynch

        self.mask_stereo_width = config_data.mask_stereo_width
        self.mask_stereo_height = config_data.mask_stereo_height
        self.mask_tof_width = config_data.mask_tof_width
        self.mask_tof_height = config_data.mask_tof_height
        self.mask_height = config_data.mask_height
        self.mask_width = config_data.mask_width
        self.downsampling = dict()
        for k, sensor_ in enumerate(config_data.input):
            self.downsampling[sensor_] = config_data.downsampling[k]

        self.min_depth_stereo = config_data.min_depth_stereo
        self.max_depth_stereo = config_data.max_depth_stereo
        self.min_depth_tof = config_data.min_depth_tof
        self.max_depth_tof = config_data.max_depth_tof
        self.min_depth = config_data.min_depth
        self.max_depth = config_data.max_depth

        self.transform = config_data.transform
        self.pad = config_data.pad

        self.scene_list = config_data.scene_list
        self.input = config_data.input
        self.target = config_data.target
        self.mode = config_data.mode

        self.filtering_model = config_data.filtering_model

        self._scenes = []

        self.sensor_line_mapping = {
            "left_depth_gt": 0,
            "left_rgb": -2,
            "left_camera_matrix": -1,
            "tof": 1,
            "tof_2": 1,
            "stereo": 2,
            "sgm_stereo": 3,
        }

        self._load_color()
        self._load_depth_gt()
        self._load_cameras()

        self._load_depths()

    def _load_depths(self):  # loads the paths of the noisy depth images to a list

        # reading files from list
        self.depth_images = dict()
        for sensor_ in self.input:  # initialize empty lists
            self.depth_images[sensor_] = []

        with open(os.path.join(self.root_dir, self.scene_list), "r") as scene_list:
            for line in scene_list:
                line = line.split(" ")
                for sensor_ in self.input:
                    files = glob.glob(
                        os.path.join(
                            self.root_dir,
                            line[self.sensor_line_mapping[sensor_]],
                            "*.png",
                        )
                    )
                    for file in files:
                        self.depth_images[sensor_].append(file)

        # sort frame order
        for sensor_ in self.depth_images.keys():
            self.depth_images[sensor_] = sorted(
                self.depth_images[sensor_],
                key=lambda x: int(os.path.splitext(x.split("/")[-1])[0]),
            )

        if self.mode == "val":
            for sensor_ in self.depth_images.keys():
                self.depth_images[sensor_] = self.depth_images[sensor_][::10]

    def _load_depth_gt(
        self,
    ):  # loads the paths of the ground truth depth images to a list
        self.depth_images_gt = []

        # reading files from list
        with open(os.path.join(self.root_dir, self.scene_list), "r") as file:
            for line in file:
                line = line.split(" ")
                if (
                    line[self.sensor_line_mapping["left_depth_gt"]].split("/")[0]
                    not in self._scenes
                ):
                    self._scenes.append(
                        line[self.sensor_line_mapping["left_depth_gt"]].split("/")[0]
                    )
                files = glob.glob(
                    os.path.join(
                        self.root_dir,
                        line[self.sensor_line_mapping["left_depth_gt"]],
                        "*.png",
                    )
                )
                for file in files:
                    self.depth_images_gt.append(file)

        self.depth_images_gt = sorted(
            self.depth_images_gt,
            key=lambda x: int(os.path.splitext(x.split("/")[-1])[0]),
        )

        if self.mode == "val":
            self.depth_images_gt = self.depth_images_gt[::10]

    def _load_color(self):
        self.color_images = []

        if self.input[0].endswith("aug"):
            rgb_path = "left_rgb_aug"
        else:
            rgb_path = "left_rgb"
        # reading files from list
        with open(os.path.join(self.root_dir, self.scene_list), "r") as file:
            for line in file:
                line = line.split(" ")
                files = glob.glob(
                    os.path.join(
                        self.root_dir,
                        line[self.sensor_line_mapping[rgb_path]],
                        "*.png",
                    )
                )
                for file in files:
                    self.color_images.append(file)

        self.color_images = sorted(
            self.color_images,
            key=lambda x: int(os.path.splitext(x.split("/")[-1])[0]),
        )

        if self.mode == "val":
            self.color_images = self.color_images[::10]

    def _load_cameras(self):
        self.cameras = []

        with open(os.path.join(self.root_dir, self.scene_list), "r") as file:

            for line in file:
                line = line.split(" ")
                files = glob.glob(os.path.join(self.root_dir, line[-1][:-1], "*.txt"))
                for file in files:
                    self.cameras.append(file)

        self.cameras = sorted(
            self.cameras, key=lambda x: int(os.path.splitext(x.split("/")[-1])[0])
        )

        if self.mode == "val":
            self.cameras = self.cameras[::10]

    @property
    def scenes(self):
        return self._scenes

    def __len__(self):
        return len(self.depth_images_gt)

    def __getitem__(self, item):

        sample = dict()
        sample["item_id"] = item

        # load rgb image
        file = self.color_images[item]
        sample["item"] = item

        pathsplit = file.split("/")
        scene = pathsplit[-4]
        trajectory = pathsplit[-3]
        frame = os.path.splitext(pathsplit[-1])[0]

        frame_id = "{}/{}/{}".format(scene, trajectory, frame)

        image = io.imread(file)

        step_x = image.shape[0] / self.resolution[0]
        step_y = image.shape[1] / self.resolution[0]

        index_y = [int(step_y * i) for i in range(0, int(image.shape[1] / step_y))]
        index_x = [int(step_x * i) for i in range(0, int(image.shape[0] / step_x))]

        image = image[:, index_y]
        image = image[index_x, :]
        sample["image"] = np.asarray(image).astype(np.float32) / 255

        intensity = rgb2gray(image)  # seems to be in range 0 - 1
        sample["intensity"] = np.asarray(intensity).astype(np.float32)
        grad_y = filters.sobel_h(intensity)
        grad_x = filters.sobel_v(intensity)
        grad = (grad_x ** 2 + grad_y ** 2) ** (1 / 2)
        sample["gradient"] = np.asarray(grad).astype(np.float32)

        # load noisy depth maps
        for sensor_ in self.input:
            if int(frame) % self.downsampling[sensor_] == 0:
                if (
                    self.filtering_model == "tsdf_early_fusion"
                    or self.filtering_model
                    == 2  # when training the routing network for the asynchronous experiment
                    and self.asynch
                    and sensor_.endswith("tof")
                ):  # for tsdf_early_fusion asynchronous experiment
                    assert self.downsampling[sensor_] == 1
                    frame_tof = (
                        int(frame) - int(frame) % 3
                    )  # two is the downsampling of the ToF sensor
                    file = self.depth_images[sensor_][item]
                    file = "/".join(file.split("/")[:-1])
                    file = file + "/" + str(frame_tof) + ".png"
                else:
                    file = self.depth_images[sensor_][item]
                depth = io.imread(file).astype(np.float32)

                try:
                    step_x = depth.shape[0] / eval("self.resolution_" + sensor_ + "[0]")
                    step_y = depth.shape[1] / eval("self.resolution_" + sensor_ + "[1]")
                except AttributeError:  # default values used in case sensor specific parameters do not exist
                    step_x = depth.shape[0] / self.resolution[0]
                    step_y = depth.shape[1] / self.resolution[1]

                index_y = [
                    int(step_y * i) for i in range(0, int(depth.shape[1] / step_y))
                ]
                index_x = [
                    int(step_x * i) for i in range(0, int(depth.shape[0] / step_x))
                ]

                depth = depth[:, index_y]
                depth = depth[index_x, :]

                depth /= 1000.0

                if (
                    self.filtering_model == "tsdf_early_fusion"
                    or self.filtering_model
                    == 2  # when training the routing network for the asynchronous experiment
                    and self.asynch
                    and sensor_.endswith("tof")
                ):  # for tsdf_early_fusion asynchronous experiment
                    if int(frame) % 3 != 0:
                        sample[sensor_ + "_depth"] = self.project_depth(
                            depth, item, frame_tof
                        )
                    else:
                        sample[sensor_ + "_depth"] = np.asarray(depth)
                else:
                    sample[sensor_ + "_depth"] = np.asarray(depth)

                if sensor_.endswith("stereo"):
                    # load right rgb image
                    file = self.color_images[item]
                    file = (
                        "/".join(file.split("/")[:-2])
                        + "/right_rgb/"
                        + file.split("/")[-1]
                    )

                    image = io.imread(file)

                    step_x = image.shape[0] / self.resolution[0]
                    step_y = image.shape[1] / self.resolution[0]

                    index_y = [
                        int(step_y * i) for i in range(0, int(image.shape[1] / step_y))
                    ]
                    index_x = [
                        int(step_x * i) for i in range(0, int(image.shape[0] / step_x))
                    ]

                    image = image[:, index_y]
                    image = image[index_x, :]
                    right_image = np.asarray(image).astype(np.float32) / 255

                    sample["right_warped_rgb_stereo"] = self.get_warped_image(
                        right_image, sample[sensor_ + "_depth"]
                    )

                    # plt.imsave('rgbwarp' +frame +'.png', sample['right_warped_rgb_stereo'])
                    # plt.imsave('left' +frame +'.png', sample['image'])
                    # plt.imsave('rgbwarpdiff' +frame +'.png', np.abs(sample['image'] - sample['right_warped_rgb_stereo']))
                    # plt.imsave('depth' +frame +'.png', sample[sensor_ + '_depth'])

                # define mask
                if (
                    not self.filtering_model == "tsdf_early_fusion"
                    and not self.filtering_model == 2
                ):
                    try:
                        mask = depth > eval("self.min_depth_" + sensor_)
                        mask = np.logical_and(
                            mask, depth < eval("self.max_depth_" + sensor_)
                        )

                        # do not integrate depth values close to the image boundary
                        mask[0 : eval("self.mask_" + sensor_ + "_height"), :] = 0
                        mask[-eval("self.mask_" + sensor_ + "_height") : -1, :] = 0
                        mask[:, 0 : eval("self.mask_" + sensor_ + "_width")] = 0
                        mask[:, -eval("self.mask_" + sensor_ + "_width") : -1] = 0
                        sample[sensor_ + "_mask"] = mask
                    except AttributeError:
                        mask = depth > self.min_depth
                        mask = np.logical_and(mask, depth < self.max_depth)

                        # do not integrate depth values close to the image boundary
                        mask[0 : self.mask_height, :] = 0
                        mask[-self.mask_height : -1, :] = 0
                        mask[:, 0 : self.mask_width] = 0
                        mask[:, -self.mask_width : -1] = 0
                        sample[sensor_ + "_mask"] = mask

        if self.filtering_model == "tsdf_early_fusion" or self.filtering_model == 2:
            mask_min = np.zeros_like(sample[self.input[0] + "_depth"])
            mask_max = np.zeros_like(sample[self.input[0] + "_depth"])
            for sensor_ in self.input:
                mask_min = np.logical_or(
                    mask_min, sample[sensor_ + "_depth"] > self.min_depth
                )
                mask_max = np.logical_or(
                    mask_max, sample[sensor_ + "_depth"] < self.max_depth
                )

            mask = np.logical_and(mask_min, mask_max)

            # do not integrate depth values close to the image boundary
            # this is relevant for the stereo modality.
            mask[0 : self.mask_height, :] = 0
            mask[-self.mask_height : -1, :] = 0
            mask[:, 0 : self.mask_width] = 0
            mask[:, -self.mask_width : -1] = 0

            sample["mask"] = mask

        # load ground truth depth map
        file = self.depth_images_gt[item]
        # print(file)
        depth = io.imread(file).astype(np.float32)

        step_x = depth.shape[0] / self.resolution[0]
        step_y = depth.shape[1] / self.resolution[0]

        index_y = [int(step_y * i) for i in range(0, int(depth.shape[1] / step_y))]
        index_x = [int(step_x * i) for i in range(0, int(depth.shape[0] / step_x))]

        depth = depth[:, index_y]
        depth = depth[index_x, :]

        depth /= 1000.0

        sample[self.target] = np.asarray(depth)
        # plt.imsave('depthdiff' +frame +'.png', np.abs(sample[sensor_ + '_depth'] - sample[self.target]))
        # plt.imsave('depthgt' +frame +'.png', sample[self.target])

        # load extrinsics
        file = self.cameras[item]
        # print(file)
        extrinsics = np.loadtxt(file)
        extrinsics = np.linalg.inv(extrinsics).astype(np.float32)
        # the fusion code expects that the camera coordinate system is such that z is in the
        # camera viewing direction, y is down and x is to the right. This is achieved by a serie of rotations
        rot_180_around_y = np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]]).astype(
            np.float32
        )
        rot_180_around_z = np.array([[-1, 0, 0], [0, -1, 0], [0, 0, 1]]).astype(
            np.float32
        )
        rot_90_around_x = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]]).astype(
            np.float32
        )
        rotation = np.matmul(rot_180_around_z, rot_180_around_y)
        extrinsics = np.matmul(rotation, extrinsics[0:3, 0:4])
        extrinsics = np.linalg.inv(
            np.concatenate((extrinsics, np.array([[0, 0, 0, 1]])), axis=0)
        )
        sample["extrinsics"] = np.matmul(rot_90_around_x, extrinsics[0:3, 0:4])

        hfov = 90.0
        try:
            for sensor_ in self.input:
                f = (
                    eval("self.resolution_" + sensor_ + "[0]")
                    / 2.0
                    * (1.0 / np.tan(np.deg2rad(hfov) / 2))
                )  # I always assume square input images
                shift = eval("self.resolution_" + sensor_ + "[0]") / 2

                # load intrinsics
                intrinsics = np.asarray(
                    [[f, 0.0, shift], [0.0, f, shift], [0.0, 0.0, 1.0]]
                )

                sample["intrinsics_" + sensor_] = intrinsics
        except AttributeError:
            f = (
                self.resolution[0] / 2.0 * (1.0 / np.tan(np.deg2rad(hfov) / 2))
            )  # I always assume square input images
            shift = self.resolution[0] / 2

            # load intrinsics
            intrinsics = np.asarray([[f, 0.0, shift], [0.0, f, shift], [0.0, 0.0, 1.0]])

            sample["intrinsics"] = intrinsics

        sample["frame_id"] = frame_id

        if self.transform:
            sample = self.transform(sample)

        return sample

    def project_depth(self, depth, item_rgb, frame_tof):
        """Projects the tof depth in the variable "depth" from index frame_tof into the view from the rgb stereo depth map from index item_rgb. Returns the projected depth map as a numpy array. Only implemented for tof psmnet stereo fusion.

        Args:
            depth: tof depth map from frame_tof
            item_rgb: index of rgb stereo depth map
            frame_tof: index of tof depth map
        """
        # load rgb extrinsics
        file = self.cameras[item_rgb]
        extrinsics_rgb = np.loadtxt(file)
        extrinsics_rgb = np.linalg.inv(extrinsics_rgb).astype(np.float32)
        # the fusion code expects that the camera coordinate system is such that z is in the
        # camera viewing direction, y is down and x is to the right. This is achieved by a serie of rotations
        rot_180_around_y = np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]]).astype(
            np.float32
        )
        rot_180_around_z = np.array([[-1, 0, 0], [0, -1, 0], [0, 0, 1]]).astype(
            np.float32
        )
        rot_90_around_x = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]]).astype(
            np.float32
        )
        rotation = np.matmul(rot_180_around_z, rot_180_around_y)
        extrinsics_rgb = np.matmul(rotation, extrinsics_rgb[0:3, 0:4])
        extrinsics_rgb = np.linalg.inv(
            np.concatenate((extrinsics_rgb, np.array([[0, 0, 0, 1]])), axis=0)
        )
        extrinsics_rgb = np.matmul(rot_90_around_x, extrinsics_rgb[0:3, 0:4])

        # load tof extrinsics
        file = "/".join(file.split("/")[:-1]) + "/" + str(frame_tof) + ".txt"
        extrinsics_tof = np.loadtxt(file)
        extrinsics_tof = np.linalg.inv(extrinsics_tof).astype(np.float32)
        # the fusion code expects that the camera coordinate system is such that z is in the
        # camera viewing direction, y is down and x is to the right. This is achieved by a serie of rotations
        extrinsics_tof = np.matmul(rotation, extrinsics_tof[0:3, 0:4])
        extrinsics_tof = np.linalg.inv(
            np.concatenate((extrinsics_tof, np.array([[0, 0, 0, 1]])), axis=0)
        )
        extrinsics_tof = np.matmul(rot_90_around_x, extrinsics_tof[0:3, 0:4])

        intrinsics = dict()
        hfov = 90.0
        try:
            for sensor_ in self.input:
                f = (
                    eval("self.resolution_" + sensor_ + "[0]")
                    / 2.0
                    * (1.0 / np.tan(np.deg2rad(hfov) / 2))
                )  # I always assume square input images
                shift = eval("self.resolution_" + sensor_ + "[0]") / 2

                # load intrinsics
                intrinsics[sensor_] = np.asarray(
                    [[f, 0.0, shift], [0.0, f, shift], [0.0, 0.0, 1.0]]
                )

        except AttributeError:
            f = (
                self.resolution[0] / 2.0 * (1.0 / np.tan(np.deg2rad(hfov) / 2))
            )  # I always assume square input images
            shift = self.resolution[0] / 2

            # load intrinsics
            intrinsics[sensor_] = np.asarray(
                [[f, 0.0, shift], [0.0, f, shift], [0.0, 0.0, 1.0]]
            )

        # project depth into rgb frame
        depth = torch.from_numpy(depth)
        h, w = depth.shape
        mask = depth > 0
        n_points = h * w

        # generate frame meshgrid
        xx, yy = torch.meshgrid(
            [torch.arange(h, dtype=torch.float), torch.arange(w, dtype=torch.float)]
        )

        if torch.cuda.is_available():  # putting data on gpu
            xx = xx.cuda()
            yy = yy.cuda()
            depth = depth.cuda()
            intrinsics["tof"] = torch.from_numpy(intrinsics["tof"]).float().cuda()
            intrinsics["stereo"] = torch.from_numpy(intrinsics["stereo"]).float().cuda()
            extrinsics_tof = torch.from_numpy(extrinsics_tof).float().cuda()
            extrinsics_rgb = torch.from_numpy(extrinsics_rgb).float().cuda()

        # flatten grid coordinates and bring them to batch size
        xx = xx.contiguous().view(h * w)
        yy = yy.contiguous().view(h * w)
        zz = depth.contiguous().view(h * w)

        # mask out the 0 depth values
        xx = xx[zz > 0].unsqueeze(-1)
        yy = yy[zz > 0].unsqueeze(-1)
        zz = zz[zz > 0].unsqueeze(-1)

        # generate points in pixel space
        points_p = torch.cat((yy, xx, zz), dim=1).clone()

        # invert
        intrinsics_inv = intrinsics["tof"].inverse()

        homogenuous = torch.ones((1, points_p.shape[0]))

        if torch.cuda.is_available():  # putting data on gpu
            homogenuous = homogenuous.cuda()

        # transform points from pixel space to camera space to world space (p->c->w)
        points_p[:, 0] *= zz[:, 0]
        points_p[:, 1] *= zz[:, 0]
        points_c = torch.matmul(
            intrinsics_inv, torch.transpose(points_p, dim0=0, dim1=1)
        )

        points_c = torch.cat((points_c, homogenuous), dim=0)

        # compute transform into rgb camera view
        extrinsics_rgb = torch.cat(
            (extrinsics_rgb, torch.tensor([[0, 0, 0, 1]]).cuda()), dim=0
        )
        extrinsics_tof = torch.cat(
            (extrinsics_tof, torch.tensor([[0, 0, 0, 1]]).cuda()), dim=0
        )
        transform = torch.matmul(extrinsics_rgb.inverse(), extrinsics_tof)
        points_c_rgb = torch.matmul(transform[:3], points_c)
        depth_c_rgb = points_c_rgb[-1, :]
        # points_c_rgb = torch.transpose(points_c_rgb, dim0=1, dim1=2)[:, :, :3]

        del xx, yy, homogenuous, points_p, points_c, intrinsics_inv
        pixels_c_rgb = torch.matmul(intrinsics["stereo"], points_c_rgb)
        pixels_c_rgb = pixels_c_rgb / pixels_c_rgb[2]
        pixels_c_rgb = pixels_c_rgb[:2]

        # remove projections which fall outside of image plane
        validx1 = (
            pixels_c_rgb[0, :] <= self.resolution_stereo[0] - 0.51  # 255.49
        )  # .49 because these are floating point precision which will be rounded down when max 0.49 (reality 0.499999)
        validx2 = pixels_c_rgb[1, :] <= self.resolution_stereo[0] - 0.51  # 255.49
        validx1 = torch.logical_and(validx1, pixels_c_rgb[0, :] >= 0)
        validx2 = torch.logical_and(validx2, pixels_c_rgb[1, :] >= 0)
        valid = torch.logical_and(validx1, validx2)

        pixels_c_rgb = pixels_c_rgb[:, valid]
        depth_c_rgb = depth_c_rgb[valid]

        # we retrieve the indices of the sorted (descending order) of the
        # depth_c_rgb tensor such that this index tensor can be used
        # to sort both depth_c_rgb and pixels_c_rgb so that
        # when we create the new depth map, we will always pick the smallest
        # depth value if two warped pixels from the source image overlap
        # in the target image.

        # retrive sorting indices
        sorting_indices = torch.argsort(depth_c_rgb, dim=0, descending=False)

        # sort pixels_c_rgb and depth_c_rgb
        depth_c_rgb = depth_c_rgb[sorting_indices]
        pixels_c_rgb = pixels_c_rgb[:, sorting_indices]

        # pixels_c_rgb = pixels_c_rgb.cpu().numpy().astype(np.int16)
        pixels_c_rgb = pixels_c_rgb.round().long().cpu()

        projected_depth = torch.zeros((h, w)).float()  # .astype(np.float32)
        projected_depth[pixels_c_rgb[1, :], pixels_c_rgb[0, :]] = depth_c_rgb.cpu()

        projected_depth = projected_depth.numpy()
        # plt.imsave(
        #     "item_" + str(item_rgb) + "from_tof_frame_" + str(frame_tof) + ".png",
        #     projected_depth,
        # )
        return projected_depth

    def get_warped_image(self, right_rgb, left_depth):
        # Note: this function assumes an image input size of 256x256.
        # To make it image size adaptive, change the focal length accordingly (128)
        disp = np.divide(
            0.1 * 128,
            left_depth,
            out=np.zeros_like(left_depth),
            where=left_depth != 0.0,
        )
        # compute disparity (unit pixels) from depth (unit m) using the fact that the baseline is 0.1 m and the focal length in pixels is 128 (since our image size is 256x256)
        size = right_rgb.shape[0]  # assumes square input image

        idx_x_left = np.transpose(
            np.expand_dims(np.arange(size), 1).repeat(size, axis=1)
        )
        idx_y = np.expand_dims(np.arange(size), 1).repeat(size, axis=1)

        idx_left = np.zeros((size, size, 2)).astype(np.int)
        idx_left[:, :, 0] = idx_y
        idx_left[:, :, 1] = idx_x_left

        idx_x_right = (idx_x_left - disp).astype(np.int)
        idx_right = np.zeros((size, size, 2)).astype(np.int)
        idx_right[:, :, 0] = idx_y
        idx_right[:, :, 1] = idx_x_right

        # get mask to remove negative indices
        idx_x_valid = idx_x_right >= 0

        # remove indices in right image which are negative (outside right image)
        idx_right = idx_right[idx_x_valid, :]
        # remove the same indices amongst left indices
        idx_left = idx_left[idx_x_valid, :]

        # warp right image to left image
        right_warp = np.zeros((size, size, 3)).astype(np.float32)
        right_warp[idx_left[:, 0], idx_left[:, 1], :] = right_rgb[
            idx_right[:, 0], idx_right[:, 1], :
        ]
        # cv2.imwrite('rightwarptest.png', right_warp)  #*15)

        return right_warp

    def get_proxy_alpha_grid(self, scene):
        file = os.path.join(self.root_dir, scene, "proxy_alpha_" + scene + ".hdf")

        # read from hdf file!
        f = h5py.File(file, "r")
        voxels = np.array(f["proxy_alpha"])
        # Add padding to grid to give more room to fusion net
        voxels = np.pad(voxels, self.pad, "constant", constant_values=-1.0)

        return voxels

    def get_grid(self, scene, truncation):
        file = os.path.join(self.root_dir, scene, "sdf_" + scene + ".hdf")

        # read from hdf file!
        f = h5py.File(file, "r")
        voxels = np.array(f["sdf"]).astype(np.float16)

        voxels[voxels > truncation] = truncation
        voxels[voxels < -truncation] = -truncation
        # Add padding to grid to give more room to fusion net
        voxels = np.pad(voxels, self.pad, "constant", constant_values=-truncation)

        print(scene, voxels.shape)
        bbox = np.zeros((3, 2))
        bbox[:, 0] = f.attrs["bbox"][:, 0] - self.pad * f.attrs["voxel_size"] * np.ones(
            (1, 1, 1)
        )
        bbox[:, 1] = bbox[:, 0] + f.attrs["voxel_size"] * np.array(voxels.shape)

        return voxels, bbox, f.attrs["voxel_size"]
