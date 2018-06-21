from __future__ import print_function
import functools
import vgg, pdb, time
import tensorflow as tf, numpy as np, os, sys
import transform
from utils import get_img
from closed_form_matting import getLaplacian

# Hack to load in segmentDeepLab with testing directory structure
# TO DO: Move segmentDeepLab into src?
currentdir = os.path.dirname(os.path.abspath(__file__))
parentdir = os.path.dirname(os.path.dirname(currentdir))
sys.path.insert(0,parentdir) 

import segmentDeepLab as seg

"""
Modified from optimization for Fast Style Transfer algorithm authored by Logan Engstrom:
    https://github.com/lengstrom/fast-style-transfer
    
Modified by Handa Yang to perform deep photorealistic style transfer, following
Luan et al. (2017):
    https://arxiv.org/abs/1703.07511
    
    with modifications from Louie Yang's TF Deep Photo Style Transfer:
        https://github.com/LouieYang/deep-photo-styletransfer-tf
"""

STYLE_LAYERS = ('relu1_1', 'relu2_1', 'relu3_1', 'relu4_1', 'relu5_1')
CONTENT_LAYER = 'relu4_2'
DEVICES = 'CUDA_VISIBLE_DEVICES'

# np arr, np arr
def optimize(content_targets, style_target, style_seg,
             content_weight, style_weight, tv_weight, photo_weight,
             vgg_path, deeplab_path, resized_dir, seg_dir, matting_dir,
             epochs=2, print_iterations=1000,
             batch_size=4, save_path='saver/fns.ckpt', slow=False,
             learning_rate=1e-3, debug=False):
    
    # Function to load segmentation masks
    """ TF implementation modified from Louie Yang's deep photo style transfer. """
    def load_seg(image_seg, new_height, new_width): #, content_shape, style_shape): # Take in TF objects for segmentation maps
        color_codes = ['BLUE', 'GREEN', 'BLACK', 'WHITE', 'RED', 'YELLOW', 'GREY', 'LIGHT_BLUE', 'PURPLE']
        def _extract_mask(seg, color_str):
            if color_str == "BLUE":
                mask_r = tf.cast((seg[:, :, :, 0] < 0.1), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] < 0.1), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] > 0.9), tf.int32)
            elif color_str == "GREEN":
                mask_r = tf.cast((seg[:, :, :, 0] < 0.1), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] > 0.9), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] < 0.1), tf.int32)
            elif color_str == "BLACK":
                mask_r = tf.cast((seg[:, :, :, 0] < 0.1), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] < 0.1), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] < 0.1), tf.int32)
            elif color_str == "WHITE":
                mask_r = tf.cast((seg[:, :, :, 0] > 0.9), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] > 0.9), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] > 0.9), tf.int32)
            elif color_str == "RED":
                mask_r = tf.cast((seg[:, :, :, 0] > 0.9), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] < 0.1), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] < 0.1), tf.int32)
            elif color_str == "YELLOW":
                mask_r = tf.cast((seg[:, :, :, 0] > 0.9), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] > 0.9), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] < 0.1), tf.int32)
            elif color_str == "GREY":
                mask_r = tf.multiply(tf.cast((seg[:, :, :, 0] > 0.4), tf.int32),
                                     tf.cast((seg[:, :, :, 0] < 0.6), tf.int32))
                mask_g = tf.multiply(tf.cast((seg[:, :, :, 1] > 0.4), tf.int32),
                                     tf.cast((seg[:, :, :, 1] < 0.6), tf.int32))
                mask_b = tf.multiply(tf.cast((seg[:, :, :, 2] > 0.4), tf.int32),
                                     tf.cast((seg[:, :, :, 2] < 0.6), tf.int32))
            elif color_str == "LIGHT_BLUE":
                mask_r = tf.cast((seg[:, :, :, 0] < 0.1), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] > 0.9), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] > 0.9), tf.int32)
            elif color_str == "PURPLE":
                mask_r = tf.cast((seg[:, :, :, 0] > 0.9), tf.int32)
                mask_g = tf.cast((seg[:, :, :, 1] < 0.1), tf.int32)
                mask_b = tf.cast((seg[:, :, :, 2] > 0.9), tf.int32)
                # HY DEBUG
                print('MASK R')
                print(mask_r)
            return tf.multiply(tf.multiply(mask_r, mask_g), mask_b)

        # TF resize input image [bs, height, width, channels]
        # NOTE TO SELF: MAKE SURE THIS IS NORMALIZED BY 255
        image_seg_resized = tf.image.resize_bilinear(image_seg, (new_height, new_width))
        print('IMG RESIZED')
        print(image_seg_resized)
        image_content_masks = []
        for i in range(len(color_codes)):
            # List of image masks / segmentation channel, in [bs, h, w, c(img channel)] format
            image_content_masks.append(tf.expand_dims(_extract_mask(image_seg_resized, color_codes[i]), -1))
            print(_extract_mask(image_seg_resized, color_codes[i]))
        return image_content_masks

    # Define batch sizes and input content images
    if slow:
        batch_size = 1
    mod = len(content_targets) % batch_size
    if mod > 0:
        print("Train set has been trimmed slightly..")
        content_targets = content_targets[:-mod] 

    style_features = {}
    # Define shapes
    resize_height = 64
    resize_width = 64
    batch_shape = (batch_size,resize_height,resize_width,3) # batch size, height, width, channels
    style_shape = (1,) + style_target.shape # batch size = 1, h, w, c
    if resize_height == 256:
        nonZeros = 1623076
    elif resize_height == 64:
        nonZeros = 98596
    else:
        raise ValueError('Don\'t know what nonzero values should be for this size.')
    
    indices_shape = (batch_size, nonZeros, 2) # Temporary hardcode--dim doesn't change for 256x256 images
    coo_shape = (batch_size, nonZeros) # Temporary hardcode
    mattingN = resize_height * resize_width
    
    # precompute style features for reference style image
    with tf.Graph().as_default(), tf.device('/cpu:0'), tf.Session() as sess:
        style_image = tf.placeholder(tf.float32, shape=style_shape, name='style_image')
        style_image_pre = vgg.preprocess(style_image)
        net = vgg.net(vgg_path, style_image_pre)
        style_pre = np.array([style_target]) # This was called by get_image(), is a uint8 image
        # Below loop computes G_l[S], can compute masks M_l,c[S] here.
        style_masks = load_seg(np.expand_dims((style_seg / 255.), 0), style_shape[1], style_shape[2]) # Normalized by 255, check.
        print('STYLE MASKS IN TF PRECOMPUTE')
        print(style_masks)
        for layer in STYLE_LAYERS:
            features = net[layer].eval(feed_dict={style_image:style_pre})
            # HY DEBUG
            # INTEND TO MULTIPLY BY STYLE MASK HERE
            # NEED TO LOOP ON c per mask channel HERE
            bs, height, width, filters = features.shape
            size = features.size
            style_features[layer] = [None] * len(style_masks) # Initialize one Gram Matrix per seg mask channel per feature layer
            for c in range(len(style_masks)): # Downscale and mask feature layers
                # F_l,c[S] = F_l[S] .* M_l,c[S] <-- this needs to be computed in precompute section on reference style image
                style_mask_resized = tf.image.resize_bilinear(style_masks[c], (height,width))
                feats = tf.reshape(tf.multiply(features, style_mask_resized), (bs, height * width, filters))
                feats_T = tf.transpose(feats, perm=[0,2,1])
                style_features[layer][c] = tf.matmul(feats_T, feats).eval(feed_dict={style_image:style_pre}) / size
            # Old NP implementation without seg maps
#            features = np.reshape(features, (-1, features.shape[3]))
#            gram = np.matmul(features.T, features) / features.size
#            style_features[layer] = gram # This is G_l[S]

    with tf.Graph().as_default(), tf.Session() as sess:
        # Content images in batch
        X_content = tf.placeholder(tf.float32, shape=batch_shape, name="X_content")
        X_pre = vgg.preprocess(X_content)
        
        # Segmentation masks -- HY PROBABLY DONT NEED THIS NOW THAT WE HAVE TF SEG MAP
        Seg_content = tf.placeholder(tf.float32, shape=batch_shape, name="Seg_content")
        
        # Load in Matting Laplacian variables
        M_indices = tf.placeholder(tf.int64, shape=indices_shape, name="M_indices")
        M_coo_data = tf.placeholder(tf.float32, shape=coo_shape, name="M_coo_data")

        # precompute content features
        content_features = {}
        content_net = vgg.net(vgg_path, X_pre)
        content_features[CONTENT_LAYER] = content_net[CONTENT_LAYER]
        
        # Preds: composite image created by image transformation network
        if slow: # Gatys neural style
            preds = tf.Variable(
                tf.random_normal(X_content.get_shape()) * 0.256
            )
            preds_pre = preds
        else: # Image transformation network prediction
            preds = transform.net(X_content/255.0) # Start from transformed version of original image
            print("PREDS SHAPE")
            print(preds.get_shape)
            preds_pre = vgg.preprocess(preds)
    
        net = vgg.net(vgg_path, preds_pre) # Preprocessed composite image
        
        # Content loss computation -- unchanged from Gatys et al. (2015)
        # net: Feature layers of output image [O]
        # content_features: Feature layers of each image in batch [I]
        content_size = _tensor_size(content_features[CONTENT_LAYER])*batch_size
        assert _tensor_size(content_features[CONTENT_LAYER]) == _tensor_size(net[CONTENT_LAYER])
        content_loss = content_weight * (2 * tf.nn.l2_loss(
            net[CONTENT_LAYER] - content_features[CONTENT_LAYER]) / content_size
        )
        layer = net[CONTENT_LAYER]
        bs, height, width, filters = map(lambda i:i.value,layer.get_shape())
        # HY DEBUG
        print("PREDS_PRE")
        print(bs, height, width, filters)
        
        # Compute style losses G_l[O] for output composite image [O]
        style_losses = []
        for style_layer in STYLE_LAYERS:
            layer = net[style_layer] # F[O]
            bs, height, width, filters = map(lambda i:i.value,layer.get_shape())
            size = height * width * filters
            # Need to downscale segmap here
            # F_l,c[O] = F_l[O] .* M_l,c[I] <-- we are here
            # F_l,c[S] = F_l[S] .* M_l,c[S] <-- this needs to be computed in precompute section before
            print('STYLE MASK LOOP')
            style_loss_per_mask = 0.0
            input_masks = load_seg((Seg_content / 255.), height, width)
            for c in range(len(style_masks)): # Downscale and mask feature layers
                # F_l,c[O] = F_l[O] .* M_l,c[I] <-- we are here
                input_mask_resized = tf.image.resize_bilinear(input_masks[c], (height,width))
#                style_mask_resized = tf.image.resize_bilinear(style_masks[c], (height, width))
                feats = tf.reshape(tf.multiply(layer, input_mask_resized), (bs, height * width, filters))
                feats_T = tf.transpose(feats, perm=[0,2,1])
                grams = tf.matmul(feats_T, feats) / size # This is G_l,c[O], feed in input masks here M_l,c[I], and and loop over I input images
                style_gram = style_features[style_layer][c] # This is G_l,c[S], (Gram matrix computed in place), compute M_l,c[I] previously
                style_loss_per_mask += 2 * tf.nn.l2_loss(grams - style_gram)/style_gram.size
                # HY TO DO: NORMALIZE BY MASK AVERAGE [THIS GIVES WEIGHTED AVERAGE OF MASKED IMAGES]
#            feats = tf.reshape(layer, (bs, height * width, filters))
#            feats_T = tf.transpose(feats, perm=[0,2,1])
#            grams = tf.matmul(feats_T, feats) / size # This is G_l[O], feed in input masks here M_l,c[I], and and loop over I input images
#            style_gram = style_features[style_layer] # This is G_l[S], (Gram matrix computed in place), compute M_l,c[I] previously
            # within the append statement below is the old style loss function
#            style_losses.append(2 * tf.nn.l2_loss(grams - style_gram)/style_gram.size) # length of style layers
            style_losses.append(style_loss_per_mask)
            # HY DEBUG
            print(style_layer)
            print("Style layer: bs, height, width, filters")
            print(bs, height, width, filters)
    
        style_loss = style_weight * functools.reduce(tf.add, style_losses) / batch_size
    
        # total variation denoising
        tv_y_size = _tensor_size(preds[:,1:,:,:])
        tv_x_size = _tensor_size(preds[:,:,1:,:])
        y_tv = tf.nn.l2_loss(preds[:,1:,:,:] - preds[:,:batch_shape[1]-1,:,:])
        x_tv = tf.nn.l2_loss(preds[:,:,1:,:] - preds[:,:,:batch_shape[2]-1,:])
        tv_loss = tv_weight*2*(x_tv/tv_x_size + y_tv/tv_y_size)/batch_size

        # Photorealistic regularization term
        # NOTE TO SELF: Add flag here to include photorealism regularization or not
        """ Modified from affine_loss in photo_style.py from
        LouieYang's Deep Photo Style Transfer
        https://github.com/LouieYang/deep-photo-styletransfer-tf
        """
        photo_loss = 0.0
        for j in range(batch_size):
#            X_content_norm = X_content[j] / 255.
            for Vc in tf.unstack(preds[j], axis=-1): # Preds has already been normalized by 255. at this point
                Vc_ravel = tf.reshape(tf.transpose(Vc), [-1])
                Matting = tf.SparseTensor(M_indices[j], M_coo_data[j], (mattingN, mattingN))
                Lm = tf.matmul(tf.expand_dims(Vc_ravel, 0), tf.sparse_tensor_dense_matmul(Matting, tf.expand_dims(Vc_ravel, -1)))
                photo_loss += photo_weight*Lm/(_tensor_size(Lm) * batch_size)
    
        # Total FPST loss function
        loss = content_loss + style_loss + tv_loss + photo_loss

        # Minimze total loss using Adam
        train_step = tf.train.AdamOptimizer(learning_rate).minimize(loss)
        sess.run(tf.global_variables_initializer())
        import random
        uid = random.randint(1, 100)
        print("UID: %s" % uid)
        for epoch in range(epochs):
            num_examples = len(content_targets)
            iterations = 0
            while iterations * batch_size < num_examples:
                start_time = time.time()
                curr = iterations * batch_size
                step = curr + batch_size
                X_batch = np.zeros(batch_shape, dtype=np.float32)
                Seg_batch = np.zeros(batch_shape, dtype=np.float32)
                indices = np.zeros(indices_shape, dtype=np.int32) # Temporary, number of nonzero elements in sparse array
                coo_data = np.zeros(coo_shape, dtype=np.float64) # Temporary, size is 256**2, 256**2
                
                # Load content images and compute Matting Laplacian for each
                for j, img_p in enumerate(content_targets[curr:step]):
                   X_batch[j] = get_img(img_p, (resize_height,resize_width,3)).astype(np.float32) # Load input images
                   # Run DeepLab here
                   img_fname = img_p.split('/')[-1]
                   if not os.path.exists(os.path.join(seg_dir, img_fname)):
                       seg.main(deeplab_path, img_p, img_fname, resized_dir, seg_dir)
                   Seg_batch[j] = get_img(os.path.join(seg_dir, img_fname), (resize_height,resize_width,3)).astype(np.float32)
                   # TO DO STORE THIS STUFF!!!
                   if not os.path.exists(os.path.join(matting_dir, img_fname + '_indices.npy')):
                       indices[j], coo_data[j] = getLaplacian(X_batch[j]) # Compute Matting Laplacian
                       np.save(os.path.join(matting_dir, img_fname + '_indices.npy'), indices[j])
                       np.save(os.path.join(matting_dir, img_fname + '_coo.npy'), coo_data[j])
                   else:
                       indices[j] = np.load(os.path.join(matting_dir, img_fname + '_indices.npy'))
                       coo_data[j] = np.load(os.path.join(matting_dir, img_fname + '_coo.npy'))
                           
                           
                
                iterations += 1
                assert X_batch.shape[0] == batch_size
    
                feed_dict = {
                   X_content:X_batch,
                   Seg_content:Seg_batch,
                   M_indices:indices.astype('int64'),
                   M_coo_data:coo_data.astype('float32')
                }

                train_step.run(feed_dict=feed_dict)
                end_time = time.time()
                delta_time = end_time - start_time
                if debug:
                    print("UID: %s, batch time: %s" % (uid, delta_time))
                is_print_iter = int(iterations) % print_iterations == 0
                if slow:
                    is_print_iter = epoch % print_iterations == 0
                is_last = epoch == epochs - 1 and iterations * batch_size >= num_examples
                should_print = is_print_iter or is_last
                if should_print:
                    to_get = [style_loss, content_loss, tv_loss, photo_loss, loss, preds]
                    test_feed_dict = {
                       X_content:X_batch,
                       Seg_content:Seg_batch,
                       M_indices:indices.astype('int64'),
                       M_coo_data:coo_data.astype('float32')
                    }

                    tup = sess.run(to_get, feed_dict = test_feed_dict)
                    _style_loss,_content_loss,_tv_loss, _photo_loss, _loss,_preds = tup
                    losses = (_style_loss, _content_loss, _tv_loss, _photo_loss, _loss)
                    if slow:
                       _preds = vgg.unprocess(_preds)
                    else:
                       saver = tf.train.Saver()
                       res = saver.save(sess, save_path)
                    yield(_preds, losses, iterations, epoch)

def _tensor_size(tensor):
    from operator import mul
    return functools.reduce(mul, (d.value for d in tensor.get_shape()[1:]), 1)
