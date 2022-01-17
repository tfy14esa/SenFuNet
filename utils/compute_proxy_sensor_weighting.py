import matplotlib.pyplot as plt
import os
import numpy as np
import open3d as o3d

import matplotlib
import trimesh
import skimage.measure

matplotlib.use("Agg")


def compute_proxy_sensor_weighting_and_mesh(
    tsdfs, gt_tsdf, test_dir, weights, voxel_size, truncation, scene, mc
):
    cmap = plt.get_cmap("inferno")

    masks = dict()
    sensors = list(tsdfs.keys())

    for sensor in sensors:
        masks[sensor] = weights[sensor] > 0

    and_mask = np.logical_and(masks[sensors[0]], masks[sensors[1]])
    mask = np.logical_or(masks[sensors[0]], masks[sensors[1]])

    sensor_weighting = -np.ones_like(gt_tsdf)

    # get mask where the gt tsdf is between sensor predictions
    mask_between = np.logical_or(
        np.logical_and((tsdfs[sensors[0]] < gt_tsdf), (gt_tsdf < tsdfs[sensors[1]])),
        np.logical_and((tsdfs[sensors[1]] < gt_tsdf), (gt_tsdf < tsdfs[sensors[0]])),
    )
    # remove the voxels where only ones sensor integrates
    mask_between = np.logical_and(and_mask, mask_between)
    gt = gt_tsdf[mask_between]
    tsdf_0 = tsdfs[sensors[0]][mask_between]
    tsdf_1 = tsdfs[sensors[1]][mask_between]
    tot_err = abs(gt - tsdf_0) + abs(gt - tsdf_1)

    weighting = abs(gt - tsdf_1) / (tot_err)
    sensor_weighting[mask_between] = weighting

    # get mask where gt is not between sensor predictions
    mask_not_between = np.logical_or(
        np.logical_and((tsdfs[sensors[0]] <= gt_tsdf), (tsdfs[sensors[1]] <= gt_tsdf)),
        np.logical_and((tsdfs[sensors[1]] >= gt_tsdf), (tsdfs[sensors[0]] >= gt_tsdf)),
    )
    # remove the voxels where only ones sensor integrates
    mask_not_between = np.logical_and(and_mask, mask_not_between)

    gt = gt_tsdf[mask_not_between]
    tsdf_0 = tsdfs[sensors[0]][mask_not_between]
    tsdf_1 = tsdfs[sensors[1]][mask_not_between]
    tsdf_0_err = abs(gt - tsdf_0)
    tsdf_1_err = abs(gt - tsdf_1)
    # if sensor 0 has a smaller err sensor weighting should be 1, otherwise 0
    sensor_weighting[mask_not_between] = tsdf_0_err < tsdf_1_err
    # gt mask where only one sensor integrates
    and_mask = np.logical_and(masks[sensors[0]], masks[sensors[1]])
    mask_0 = np.logical_xor(and_mask, masks[sensors[0]])
    mask_1 = np.logical_xor(and_mask, masks[sensors[1]])
    tsdf_0 = tsdfs[sensors[0]][mask_0]
    tsdf_1 = tsdfs[sensors[1]][mask_1]
    tsdf_0_err = abs(gt_tsdf[mask_0] - tsdf_0)
    tsdf_1_err = abs(gt_tsdf[mask_1] - tsdf_1)

    # if sensor 0 has an error of more than 0.04 we make the alpha 0
    sensor_weighting[mask_0] = tsdf_0_err < 0.04
    # if sensor 1 has an error of more than 0.04 we make the alpha 1
    sensor_weighting[mask_1] = tsdf_1_err > 0.04

    one_sensor_mask = np.logical_or(mask_0, mask_1)

    # save proxy sensor weighting as an hdf file
    # import h5py
    # with h5py.File('proxy_alpha.hdf', 'w') as hf:
    # 	hf.create_dataset("proxy_alpha",
    # 	shape=sensor_weighting.shape,
    # 	data=sensor_weighting,
    # 	compression='gzip',
    # 	compression_opts=9)

    # plot tsdf fusion histogram
    # sensor_weighting_path = "/cluster/work/cvl/esandstroem/src/late_fusion_3dconvnet/workspace/fusion/211019-170325/test_no_carving/hotel_0.sensor_weighting.hf5"
    # import h5py

    # f = h5py.File(sensor_weighting_path, "r")
    # sensor_weighting_tsdf_middle_fusion = np.array(f["sensor_weighting"]).astype(
    #     np.float16
    # )
    # hist = sensor_weighting_tsdf_middle_fusion[mask].flatten()

    # n, bins, patches = plt.hist(hist, bins=100)
    # for c, p in zip(bins, patches):
    #     plt.setp(p, "facecolor", cmap(c))
    # plt.savefig(
    #     test_dir + "/tsdf_fusion_sensor_weighting_grid_histogram" + scene + ".png"
    # )

    plt.clf()  # clear plot (important)

    hist = sensor_weighting[mask].flatten()

    n, bins, patches = plt.hist(hist, bins=100)
    for c, p in zip(bins, patches):
        plt.setp(p, "facecolor", cmap(c))
    plt.savefig(test_dir + "/proxy_sensor_weighting_grid_histogram" + scene + ".png")
    plt.clf()

    hist = sensor_weighting[mask_between].flatten()

    n, bins, patches = plt.hist(hist, bins=100)
    for c, p in zip(bins, patches):
        plt.setp(p, "facecolor", cmap(c))
    plt.savefig(
        test_dir
        + "/proxy_sensor_weighting_grid_histogram_mask_between"
        + scene
        + ".png"
    )
    plt.clf()

    hist = sensor_weighting[mask_not_between].flatten()

    n, bins, patches = plt.hist(hist, bins=100)
    for c, p in zip(bins, patches):
        plt.setp(p, "facecolor", cmap(c))
    plt.savefig(
        test_dir
        + "/proxy_sensor_weighting_grid_histogram_mask_not_between"
        + scene
        + ".png"
    )
    plt.clf()

    hist = sensor_weighting[one_sensor_mask].flatten()

    n, bins, patches = plt.hist(hist, bins=100)
    for c, p in zip(bins, patches):
        plt.setp(p, "facecolor", cmap(c))
    plt.savefig(
        test_dir
        + "/proxy_sensor_weighting_grid_histogram_mask_one_sensor"
        + scene
        + ".png"
    )
    plt.clf()

    # compute filtered tsdf grid
    filtered_tsdf = (
        sensor_weighting * tsdfs[sensors[0]]
        + (1 - sensor_weighting) * tsdfs[sensors[1]]
    )

    # get mesh from filtered_grid

    # first filter or mask according to the sensor weighting
    only_one_sensor_mask = np.logical_xor(mask, and_mask)
    for sensor_ in sensors:
        only_sensor_mask = np.logical_and(only_one_sensor_mask, masks[sensor_])
        if sensor_ == sensors[0]:
            rem_indices = np.logical_and(only_sensor_mask, sensor_weighting < 0.5)
        else:
            rem_indices = np.logical_and(only_sensor_mask, sensor_weighting > 0.5)

        mask[rem_indices] = 0

    # eval_results_scene = evaluation(tsdf, sdf_gt, mask)

    # logger.info('Test Scores for scene: ' + scene)
    # for key in eval_results_scene:
    #     logger.info(key + ': ' + str(eval_results_scene[key]))
    # mask = and_mask

    # Create the mesh using the given mask
    resolution = gt_tsdf.shape
    max_resolution = np.array(resolution).max()
    length = (max_resolution) * voxel_size

    if mc == "Open3D":
        tsdf_cube = np.zeros((max_resolution, max_resolution, max_resolution))
        tsdf_cube[: resolution[0], : resolution[1], : resolution[2]] = filtered_tsdf

        indices_x = mask.nonzero()[0]
        indices_y = mask.nonzero()[1]
        indices_z = mask.nonzero()[2]

        volume = o3d.integration.UniformTSDFVolume(
            length=length,
            resolution=max_resolution,
            sdf_trunc=truncation,
            color_type=o3d.integration.TSDFVolumeColorType.RGB8,
        )

        for i in range(indices_x.shape[0]):
            volume.set_tsdf_at(
                tsdf_cube[indices_x[i], indices_y[i], indices_z[i]],
                indices_x[i],
                indices_y[i],
                indices_z[i],
            )
            volume.set_weight_at(1, indices_x[i], indices_y[i], indices_z[i])

        print("Extract a triangle mesh from the volume and visualize it.")
        mesh = volume.extract_triangle_mesh()

        del volume
        mesh.compute_vertex_normals()
        # o3d.visualization.draw_geometries([mesh])

        vertices = mesh.vertices
    elif mc == "skimage":
        (verts, faces, normals, values) = skimage.measure.marching_cubes_lewiner(
            filtered_tsdf,
            level=0,
            spacing=(voxel_size, voxel_size, voxel_size),
            mask=preprocess_weight_grid(mask),
        )
        # add 0.5 * voxel_size to vertices to match Open3D marching cubes output
        vertices = verts + voxel_size / 2
        mesh = o3d.geometry.TriangleMesh(
            vertices=o3d.utility.Vector3dVector(vertices),
            triangles=o3d.utility.Vector3iVector(faces),
        )
        mesh.compute_vertex_normals()

    voxel_points = np.round(
        np.asarray(vertices - voxel_size / 2) * 1 / voxel_size
    ).astype(int)

    # remove voxels if they are outside of the voxelgrid - these are treated as uninitialized.
    # this step is not needed when we subtract half a voxel size - without this the transformation
    # is wrong.
    valid_points = (
        (voxel_points[:, 0] >= 0)
        * (voxel_points[:, 0] < sensor_weighting.shape[0])
        * (voxel_points[:, 1] >= 0)
        * (voxel_points[:, 1] < sensor_weighting.shape[1])
        * (voxel_points[:, 2] >= 0)
        * (voxel_points[:, 2] < sensor_weighting.shape[2])
    )

    filtered_voxel_points = voxel_points[valid_points, :]

    vals = -np.ones(voxel_points.shape[0])
    vals[valid_points] = sensor_weighting[
        filtered_voxel_points[:, 0],
        filtered_voxel_points[:, 1],
        filtered_voxel_points[:, 2],
    ]
    colors = cmap((vals * 255).astype(int))[:, :-1]
    # print(colors.shape)
    if (vals == -1).sum() > 0:
        print((vals == -1).sum(), "invalid index or indices found among voxel points!")
        # return
    # print((vals == -1).sum()) # this sum should always be zero when we subtract half a voxel size to get to the voxel
    # coordinate space.
    colors[vals == -1] = [0, 1, 0]  # make all uninitialized voxels green

    mesh.vertex_colors = o3d.utility.Vector3dVector(colors)
    o3d.io.write_triangle_mesh(
        test_dir + "/proxy_sensor_weighting_" + scene + ".ply", mesh
    )

    # compute surface histogram histogram of "averaged" alpha values.
    cm = plt.get_cmap("inferno")
    n, bins, patches = plt.hist(vals, bins=100)
    for c, p in zip(bins, patches):
        plt.setp(p, "facecolor", cm(c))
    plt.savefig(test_dir + "/proxy_sensor_weighting_surface_histogram.png")
    plt.clf()

    # compute F-score of proxy fused mesh
    os.chdir(test_dir)
    os.system(
        "evaluate_3d_reconstruction.py "
        + "proxy_sensor_weighting_"
        + scene
        + ".ply"
        + " standard_trunc "
        + scene
    )

    return sensor_weighting


def preprocess_weight_grid(weights):
    """Function to compute the weight mask for skimage marching cubes corresponding to how Open3D marching cubes deals with masking. Open3D requires that all 8 corners of the voxel are initialized in order to draw a surface while skimage only requires 1 of the voxels to be initialized e.g. the index (1,1,1) determines if the voxel at (0,0,0) is initialized etc.

    Args:
        weights: weight grid

    Returns:
        mask: boolean grid to be used as input to skimage marching cubes algorithm
    """
    mask = np.zeros_like(weights)
    indices = np.array(weights.nonzero())
    indices = indices[:, ~np.any(indices == 0, axis=0)]
    for index in range(indices.shape[1]):
        i = indices[:, index][0]
        j = indices[:, index][1]
        k = indices[:, index][2]
        mask[i, j, k] = weights[i, j, k]
        mask[i, j, k] = mask[i, j, k] and weights[i, j, k - 1]
        mask[i, j, k] = mask[i, j, k] and weights[i, j - 1, k]
        mask[i, j, k] = mask[i, j, k] and weights[i, j - 1, k - 1]
        mask[i, j, k] = mask[i, j, k] and weights[i - 1, j, k]
        mask[i, j, k] = mask[i, j, k] and weights[i - 1, j, k - 1]
        mask[i, j, k] = mask[i, j, k] and weights[i - 1, j - 1, k]
        mask[i, j, k] = mask[i, j, k] and weights[i - 1, j - 1, k - 1]

    return mask > 0
