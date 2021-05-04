import torch
import numpy as np


class Integrator(torch.nn.Module):

    def __init__(self, config):

        super(Integrator, self).__init__()

        self.device = config.device
        self.max_weight = config.max_weight
        self.train_on_border_voxels = config.train_on_border_voxels
        self.extraction_strategy = config.extraction_strategy

    def forward(self, integrator_input, values_volume, features_volume, weights_volume, feature_weights_volume):
        xs, ys, zs = values_volume.shape
        


        # unpack data
        values = integrator_input['update_values'].to('cuda:0')
        features = integrator_input['update_features'].to('cuda:0')
        indices = integrator_input['update_indices'].to('cuda:0')
        feature_indices = integrator_input['update_feature_indices'].to('cuda:0')
        filter_indices = integrator_input['filter_indices']
        weights = integrator_input['update_weights'].to('cuda:0')
        indices_empty= integrator_input['update_indices_empty'].to('cuda:0')
        weights_empty = integrator_input['update_weights_empty'].to('cuda:0')

        n1, n2, n3, f4 = features.shape # f1 = 1, f2= 65536 (when no filtering), f3= tail_points, f4 = nbr_features

        # reshape tensors
        features = features.contiguous().view(-1, f4).float()
        values = values.contiguous().view(-1, 1).float()

        if self.extraction_strategy == 'trilinear_interpolation':
            values = values.repeat(1, 8)
            indices_no_border = filter_indices.contiguous().view(-1, 8, 3).long()
            indices = indices.contiguous().view(-1, 8, 3).long()
            weights = weights.contiguous().view(-1, 8)
            indices_empty = indices_empty.contiguous().view(-1, 8, 3).long()
            weights_empty = weights_empty.contiguous().view(-1, 8)
        elif self.extraction_strategy == 'nearest_neighbor':
            values = values.repeat(1, 1)
            indices_no_border = filter_indices.contiguous().view(-1, 1, 3).long()
            indices = indices.contiguous().view(-1, 1, 3).long()
            weights = weights.contiguous().view(-1, 1)
            indices_empty = indices_empty.contiguous().view(-1, 1, 3).long()
            weights_empty = weights_empty.contiguous().view(-1, 1)

        values = values.contiguous().view(-1, 1).float()
        indices_no_border = filter_indices.contiguous().view(-1, 3)
        indices = indices.contiguous().view(-1, 3).long()
        feature_indices = feature_indices.contiguous().view(-1, 3).long()
        indices_empty = indices_empty.contiguous().view(-1, 3).long() # (65536*7*8, 3)

        weights = weights.contiguous().view(-1, 1).float()
        weights_empty = weights_empty.contiguous().view(-1, 1).float()
        
        # get valid indices
        valid_no_border = get_index_mask(indices_no_border, values_volume.shape)
        indices_no_border = extract_indices(indices_no_border, mask=valid_no_border)

        valid = get_index_mask(indices, values_volume.shape)
        indices = extract_indices(indices, mask=valid)

        valid_empty = get_index_mask(indices_empty, values_volume.shape)
        indices_empty = extract_indices(indices_empty, mask=valid_empty)

        valid_features = get_index_mask(feature_indices, values_volume.shape)
        feature_indices = extract_indices(feature_indices, mask=valid_features)

        # remove the invalid entries from the values, features and weights
        valid_features = valid_features.clone().unsqueeze_(-1)
        features = torch.masked_select(features, valid_features.repeat(1, f4)) # (65536*7*8*6) if all indices are valid, otherwise less
        features = features.view(int(features.shape[0]/f4), f4) # (65536*7*8, 6) if all indices are valid, otherwise less
        
        values = torch.masked_select(values[:, 0], valid)
        weights = torch.masked_select(weights[:, 0], valid)
        weights_empty = torch.masked_select(weights_empty[:, 0], valid_empty)


        update_feat = features
        del features

        update = weights * values
        del values

        # aggregate updates to the same index

        # tsdf
        index = ys * zs * indices[:, 0] + zs * indices[:, 1] + indices[:, 2]
        indices_insert = torch.unique_consecutive(indices[index.sort()[1]], dim=0) # since the coalesce() operation on the sparse tensors sorts the
        vcache = torch.sparse.FloatTensor(index.unsqueeze_(0), update, torch.Size([xs * ys * zs])).coalesce()
        update = vcache.values()
        del vcache

        # weights for tsdf
        wcache = torch.sparse.FloatTensor(index, weights, torch.Size([xs * ys * zs])).coalesce() # this line adds the values at the same index together
        indices = wcache.indices().squeeze()
        weights = wcache.values()
        del wcache

        # weights for empty indices
        index_empty = ys * zs * indices_empty[:, 0] + zs * indices_empty[:, 1] + indices_empty[:, 2]
        indices_empty_insert = torch.unique_consecutive(indices_empty[index_empty.sort()[1]], dim=0) # since the coalesce() operation on the sparse tensors sorts the
        wcache_empty = torch.sparse.FloatTensor(index_empty.unsqueeze_(0), weights_empty, torch.Size([xs * ys * zs])).coalesce() # this line adds the values at the same index together
        indices_empty = wcache_empty.indices().squeeze()
        weights_empty = wcache_empty.values()
        del wcache_empty

        # remove duplicate indices that will be passed to the filtering and translation during training
        index_no_border = ys * zs * indices_no_border[:, 0] + zs * indices_no_border[:, 1] + indices_no_border[:, 2]
        indices_no_border_insert = torch.unique_consecutive(indices_no_border[index_no_border.sort()[1]], dim=0) # since the coalesce() operation on the sparse tensors sorts the

        # features
        feature_index = ys * zs * feature_indices[:, 0] + zs * feature_indices[:, 1] + feature_indices[:, 2]
        feature_indices_insert = torch.unique_consecutive(feature_indices[feature_index.sort()[1]], dim=0)
        fcache = torch.sparse.FloatTensor(feature_index.unsqueeze_(0), update_feat, torch.Size([xs * ys * zs, f4])).coalesce()
        update_feat_weights = torch.ones_like(update_feat[:, 0])
        feature_indices = fcache.indices().squeeze()
        update_feat = fcache.values()
        del fcache

        #feature weights
        wcache_feat = torch.sparse.FloatTensor(feature_index, update_feat_weights, torch.Size([xs * ys * zs])).coalesce()
        weights_feat = wcache_feat.values().unsqueeze_(-1).repeat(1, f4).float()
        del wcache_feat

        # tsdf and weights update
        values_old = values_volume.view(xs * ys * zs)[indices]
        weights_old = weights_volume.view(xs * ys * zs)[indices]
        value_update = (weights_old * values_old + update) / (weights_old + weights)
        weight_update = weights_old + weights
        weight_update = torch.clamp(weight_update, 0, self.max_weight)

        # empty space update
        values_old_empty = values_volume.view(xs * ys * zs)[indices_empty]
        weights_old_empty = weights_volume.view(xs * ys * zs)[indices_empty]
        value_update_empty = torch.add(weights_old_empty * values_old_empty, 0.1*weights_empty) / (weights_old_empty + weights_empty)
        weight_update_empty = weights_old_empty + weights_empty
        weight_update_empty = torch.clamp(weight_update_empty, 0, self.max_weight)

        # feature update
        feature_weights_old = feature_weights_volume.view(xs * ys * zs)[feature_indices].unsqueeze_(-1).repeat(1, f4).float()
        features_old = features_volume.view(xs * ys * zs, f4)[feature_indices]

        # here we should not multiply the update_feat with weights_feat in the nominator since we already have that baked in
        # as weight one for each term when we sum all features to the index
        feature_update = (feature_weights_old * features_old + update_feat) / (feature_weights_old + weights_feat)
        weight_update_features = feature_weights_old + weights_feat
        weight_update_features = torch.clamp(weight_update_features, 0, self.max_weight)
        # we need to reduce the size of the tensor since the tensor contains 4 copies of each weight
        weight_update_features = weight_update_features[:, 0]

        del update_feat, feature_weights_old, weights_feat

        # we need to threshold the weight update and value updates only for weight updates at least 0.01 because we will other
        # wise, due to the half() operation, but those updates to 0, which is not good at all. In fact it is e-07 but I take 0.01. Nope we 
        # do not need to do this if we only use the feature indices since they always have at least weight 1 so the half() does 
        # not cause any numerical issues for the subsequent filtereing and translation nets. I had to do this in order to avoid feeding neighborhoods
        # where both central voxels had 0 weight. In the unlikely event that  this happened, when I neglect attention to uninitialized voxels,
        # all scores will be -inf and the softmax will give nan as output.

        # inser tsdf and tsdf weights
        insert_values(value_update, indices_insert, values_volume)
        insert_values(weight_update, indices_insert, weights_volume)

        # print(feature_volume.sum(dim))
        # insert features and feature weights
        insert_values(feature_update, feature_indices_insert, features_volume) 
        insert_values(weight_update_features, feature_indices_insert, feature_weights_volume) 

        insert_values(value_update_empty, indices_empty_insert, values_volume)
        insert_values(weight_update_empty, indices_empty_insert, weights_volume)

        # I don't need to threshold the behind the surface online outlier filter wrt to the weights
        # because I don't update the weights at all. But we should not update these indices if they
        # are already initialized because this will create artifacts behind objects - it will smear
        # out the reconstruction behind objects a lot so this update can only happen if the voxel has weight 0

        # extract indices that are not initialized among the indices in the variable 
        # indices_empty_behind
        # valid_indices = (weights_volume[indices_empty_behind[:, 0], indices_empty_behind[:, 1], indices_empty_behind[:, 2]] == 0).nonzero()[:, 0]
        # insert_values(value_update_empty_behind[valid_indices], indices_empty_behind[valid_indices], values_volume)

        if self.train_on_border_voxels:
            indices = torch.cat((indices, indices_empty[:int(indices_empty.shape[0]/10), :]), dim=0)
        else:
            # we need to feed the weights belonging to the no border voxels in order to not train the filtering and translation
            # networks on center voxels with a zero weight. On the other hand, if I only train the filtering and translation net using the 
            # feature indices (which always have a weight of at least 1 for each update), then I don't need this step.
            # valid_filter = weight_update_filter > 0.01
            # indices_no_border = indices_no_border_insert[valid_filter, :]

            indices = indices_no_border_insert
            #indices = torch.cat((indices_no_border, indices_empty[:int(indices_empty.shape[0]/10), :]), dim=0)

        return values_volume, features_volume, weights_volume, feature_weights_volume, indices

    def forward_old(self, integrator_input, values_volume, features_volume, weights_volume):
        xs, ys, zs = values_volume.shape

        # reshape tensors
        # n1, n2, n3 = values.shape
        # print(filter_indices.shape)
        values = integrator_input['update_values'].to('cuda:0')
        features = integrator_input['update_features'].to('cuda:0')
        indices = integrator_input['update_indices'].to('cuda:0')
        filter_indices = integrator_input['filter_indices']
        filter_weights = integrator_input['filter_weights']
        weights = integrator_input['update_weights'].to('cuda:0')
        indices_empty= integrator_input['update_indices_empty'].to('cuda:0')
        weights_empty = integrator_input['update_weights_empty'].to('cuda:0')

        n1, n2, n3, f4 = features.shape # f1 = 1, f2= 65536 (when no filtering), f3= tail_points, f4 = nbr_features

        features = features.contiguous().view(-1, f4, 1)
        features = features.repeat(1, 1, 8)


        values = values.contiguous().view(-1, 1)
        values = values.repeat(1, 8)

        # indices = indices.contiguous().view(n1 * n2 * n3, 8, 3).long()
        # weights = weights.contiguous().view(n1 * n2 * n3, 8)
        # filter_weights = filter_weights.contiguous().view(-1, 8)
        # indices_empty = indices_empty.contiguous().view(-1, 8, 3).long()
        # weights_empty = weights_empty.contiguous().view(-1, 8)
        # indices_empty_behind = indices_empty_behind.contiguous().view(-1, 8, 3).long()
        # weights_empty_behind = weights_empty_behind.contiguous().view(-1, 8)

        # n1, n2, n3 = indices.shape
        indices_no_border = filter_indices.contiguous().view(-1, 3)
        indices = indices.contiguous().view(-1, 3).long()

        weights = weights.contiguous().view(-1, 1).float()
        filter_weights = filter_weights.contiguous().view(-1, 1).float()
        indices_empty = indices_empty.contiguous().view(-1, 3).long() # (65536*7*8, 3)
        weights_empty = weights_empty.contiguous().view(-1, 1).float()
        # indices_empty_behind = indices_empty_behind.contiguous().view(-1, n3).long() # (65536*7*8, 3)
        # weights_empty_behind = weights_empty_behind.contiguous().view(-1, 1).float()
        values = values.contiguous().view(-1, 1).float()
        features = features.contiguous().view(-1, f4).float() # (65536*7*8, 6)

        valid_no_border = get_index_mask(indices_no_border, values_volume.shape)
        valid = get_index_mask(indices, values_volume.shape)
        valid_empty = get_index_mask(indices_empty, values_volume.shape)
        # valid_empty_behind = get_index_mask(indices_empty_behind, values_volume.shape)

        indices_no_border = extract_indices(indices_no_border, mask=valid_no_border)

        feature_valid = valid.clone().unsqueeze_(-1)
        features = torch.masked_select(features, feature_valid.repeat(1, f4)) # (65536*7*8*6) if all indices are valid, otherwise less

        features = features.view(int(features.shape[0]/f4), f4) # (65536*7*8, 6) if all indices are valid, otherwise less
        
        values = torch.masked_select(values[:, 0], valid)
        indices = extract_indices(indices, mask=valid)
        indices_empty = extract_indices(indices_empty, mask=valid_empty)
        # indices_empty_behind = extract_indices(indices_empty_behind, mask=valid_empty_behind)
        weights = torch.masked_select(weights[:, 0], valid)
        filter_weights = torch.masked_select(filter_weights[:, 0], valid_no_border)
        weights_empty = torch.masked_select(weights_empty[:, 0], valid_empty)

        feature_weights = weights.clone().unsqueeze_(-1).repeat(1, f4)

        update_feat = feature_weights * features
        del feature_weights, features
        # weights_empty_behind = torch.masked_select(weights_empty_behind[:, 0], valid_empty_behind)

        update = weights * values

        index_no_border = ys * zs * indices_no_border[:, 0] + zs * indices_no_border[:, 1] + indices_no_border[:, 2]
        indices_no_border = torch.unique_consecutive(indices_no_border[index_no_border.sort()[1]], dim=0) # since the coalesce() operation on the sparse tensors sorts the

        index = ys * zs * indices[:, 0] + zs * indices[:, 1] + indices[:, 2]
        indices = torch.unique_consecutive(indices[index.sort()[1]], dim=0) # since the coalesce() operation on the sparse tensors sorts the
        wcache = torch.sparse.FloatTensor(index.unsqueeze_(0), weights, torch.Size([xs * ys * zs])).coalesce() # this line adds the values at the same index together
        index_unique = wcache.indices().squeeze()
        weights = wcache.values()

        del wcache


        index_empty = ys * zs * indices_empty[:, 0] + zs * indices_empty[:, 1] + indices_empty[:, 2]
        indices_empty = torch.unique_consecutive(indices_empty[index_empty.sort()[1]], dim=0) # since the coalesce() operation on the sparse tensors sorts the

        wcache_empty = torch.sparse.FloatTensor(index_empty.unsqueeze_(0), weights_empty, torch.Size([xs * ys * zs])).coalesce() # this line adds the values at the same index together
        weights_empty = wcache_empty.values()

        del wcache_empty

        # index_empty_behind = ys * zs * indices_empty_behind[:, 0] + zs * indices_empty_behind[:, 1] + indices_empty_behind[:, 2]
        # indices_empty_behind = torch.unique_consecutive(indices_empty_behind[index_empty_behind.sort()[1]], dim=0) # since the coalesce() operation on the sparse tensors sorts the

        # wcache_empty_behind = torch.sparse.FloatTensor(index_empty_behind.unsqueeze_(0), weights_empty_behind, torch.Size([xs * ys * zs])).coalesce() # this line adds the values at the same index together
        # weights_empty_behind = wcache_empty_behind.values()

        # del wcache_empty_behind

        wcache_filter = torch.sparse.FloatTensor(index_no_border.unsqueeze_(0), filter_weights, torch.Size([xs * ys * zs])).coalesce() # this line adds the values at the same index together
        filter_weights = wcache_filter.values()

        del wcache_filter

        fcache = torch.sparse.FloatTensor(index, update_feat, torch.Size([xs * ys * zs, f4])).coalesce()

        update_feat = fcache.values()
        del fcache

        feature_weights= weights.clone()
        feature_weights = feature_weights.unsqueeze_(-1)
        feature_weights = feature_weights.repeat(1, f4)

        vcache = torch.sparse.FloatTensor(index, update, torch.Size([xs * ys * zs])).coalesce()
        update = vcache.values()
        del vcache
        
        values_old = extract_values(indices, values_volume)
        weights_old = extract_values(indices, weights_volume)

        values_old_empty = extract_values(indices_empty, values_volume)
        weights_old_empty = extract_values(indices_empty, weights_volume)

        # values_old_empty_behind = extract_values(indices_empty_behind, values_volume)
        # weights_old_empty_behind = extract_values(indices_empty_behind, weights_volume)
        weights_old_filter = extract_values(indices_no_border, weights_volume)

        value_update = (weights_old * values_old + update) / (weights_old + weights)
        value_update_empty = torch.add(weights_old_empty * values_old_empty, 0.1*weights_empty) / (weights_old_empty + weights_empty)
        # value_update_empty_behind = torch.add(weights_old_empty_behind * values_old_empty_behind, -0.1*weights_empty_behind) / (weights_old_empty_behind + weights_empty_behind)


        weight_update = weights_old + weights
        weight_update = torch.clamp(weight_update, 0, self.max_weight)
        weight_update_empty = weights_old_empty + weights_empty
        weight_update_empty = torch.clamp(weight_update_empty, 0, self.max_weight)

        weight_update_filter = weights_old_filter + filter_weights

        feature_weights= weights.clone()
        feature_weights = feature_weights.unsqueeze_(-1)
        feature_weights = feature_weights.repeat(1, f4)
        
        feature_weights_old = weights_old.clone().unsqueeze_(-1).repeat(1, f4).float()
        features_old = features_volume.view(xs * ys * zs, f4)[index_unique]

        feature_update = (feature_weights_old * features_old + update_feat) / (feature_weights_old + feature_weights)
        del update_feat, feature_weights_old, feature_weights

        # we need to threshold the weight update and value updates only for weight updates at least 0.01 because we will other
        # wise, due to the half() operation, but those updates to 0, which is not good at all. In fact it is e-07 but I take 0.01.

        valid = weight_update > 0.01
        value_update = value_update[valid]
        weight_update = weight_update[valid]
        feature_update = feature_update[valid, :]
        indices = indices[valid, :]

        insert_values(value_update, indices, values_volume)
        insert_values(feature_update, indices, features_volume) 
        insert_values(weight_update, indices, weights_volume)

        valid_empty = weight_update_empty > 0.01
        value_update_empty = value_update_empty[valid_empty]
        weight_update_empty = weight_update_empty[valid_empty]
        indices_empty = indices_empty[valid_empty, :]

        insert_values(value_update_empty, indices_empty, values_volume)
        insert_values(weight_update_empty, indices_empty, weights_volume)

        # I don't need to threshold the behind the surface online outlier filter wrt to the weights
        # because I don't update the weights at all. But we should not update these indices if they
        # are already initialized because this will create artifacts behind objects - it will smear
        # out the reconstruction behind objects a lot so this update can only happen if the voxel has weight 0

        # extract indices that are not initialized among the indices in the variable 
        # indices_empty_behind
        # valid_indices = (weights_volume[indices_empty_behind[:, 0], indices_empty_behind[:, 1], indices_empty_behind[:, 2]] == 0).nonzero()[:, 0]
        # insert_values(value_update_empty_behind[valid_indices], indices_empty_behind[valid_indices], values_volume)

        if self.train_on_border_voxels:
            indices = torch.cat((indices, indices_empty[:int(indices_empty.shape[0]/10), :]), dim=0)
        else:
            valid_filter = weight_update_filter > 0.01
            indices_no_border = indices_no_border[valid_filter, :]

            indices = indices_no_border
            #indices = torch.cat((indices_no_border, indices_empty[:int(indices_empty.shape[0]/10), :]), dim=0)



        return values_volume, features_volume, weights_volume, indices


def get_index_mask(indices, shape):
    """
    method to check whether indices are valid
    :param indices: indices to check
    :param shape: constraints for indices
    :return: mask
    """
    xs, ys, zs = shape

    valid = ((indices[:, 0] >= 0) &
             (indices[:, 0] < xs) &
             (indices[:, 1] >= 0) &
             (indices[:, 1] < ys) &
             (indices[:, 2] >= 0) &
             (indices[:, 2] < zs))

    return valid




def extract_values(indices, volume, mask=None):
    """
    method to extract values from volume given indices
    :param indices: positions to extract
    :param volume: volume to extract from
    :param mask: optional mask for extraction
    :return: extracted values
    """

    if mask is not None:
        x = torch.masked_select(indices[:, 0], mask)
        y = torch.masked_select(indices[:, 1], mask)
        z = torch.masked_select(indices[:, 2], mask)
    else:
        x = indices[:, 0]
        y = indices[:, 1]
        z = indices[:, 2]

    return volume[x, y, z]


def extract_indices(indices, mask):
    """
    method to extract indices according to mask
    :param indices:
    :param mask:
    :return:
    """

    x = torch.masked_select(indices[:, 0], mask)
    y = torch.masked_select(indices[:, 1], mask)
    z = torch.masked_select(indices[:, 2], mask)

    masked_indices = torch.cat((x.unsqueeze_(1),
                                y.unsqueeze_(1),
                                z.unsqueeze_(1)), dim=1)
    return masked_indices


def insert_values(values, indices, volume):
    """
    method to insert values back into volume
    :param values:
    :param indices:
    :param volume:
    :return:
    """
    # print(volume.dtype)
    # print(values.dtype)
    if volume.dim() == 3:
        volume = volume.half()
        # indices = indices[~torch.isnan(values)] # remove those indices where values = nan (due to round off error when confidence is too low)
        # values = values[~torch.isnan(values)] # remove those values where values = nan (due to round off error when confidence is too low)
        volume[indices[:, 0], indices[:, 1], indices[:, 2]] = values.half()
    else:
        volume = volume.half()
        # indices = indices[~torch.isnan(values)] # remove those indices where values = nan (due to round off error when confidence is too low)
        # values = values[~torch.isnan(values)] # remove those values where values = nan (due to round off error when confidence is too low)
        volume[indices[:, 0], indices[:, 1], indices[:, 2], :] = values.half()
