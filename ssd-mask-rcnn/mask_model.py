#coding=utf-8
import tensorflow as tf
from tensorflow.contrib import slim
import utils
import data_gen
import cv2
import numpy as np
import np_utils
import visual
import time
import glob
from models import mask_iv2

def loss_clc(truth_box,gt_lables):
    true_box, non_ze = utils.trim_zeros_graph(truth_box)
    gt_lables = tf.boolean_mask(gt_lables, non_ze)
    priors = utils.get_prio_box()
    priors_xywh = tf.concat((priors[:, :2] - priors[:, 2:] / 2,
               priors[:, :2] + priors[:, 2:] / 2), axis=1)
    ops = utils.overlaps_graph(true_box,priors_xywh)
    # 获取prior box 最佳匹配index size = trueth
    best_prior_idx = tf.argmax(ops,axis=1)
    # 获取truth box 最佳匹配index size = prior
    best_truth_idx = tf.argmax(ops,axis=0)
    best_truth_overlab = tf.reduce_max(ops,axis=0)
    matches = tf.gather(truth_box,best_truth_idx)
    conf = tf.gather(gt_lables,best_truth_idx)
    conf = tf.cast(conf,tf.float32)
    zer = tf.zeros(shape=(tf.shape(conf)),dtype=tf.float32)
    conf = tf.where(tf.less(best_truth_overlab,0.5),zer,conf)
    loc = utils.encode_box(matched=matches,prios=priors)
    return conf,loc


def smooth_l1_loss(y_true, y_pred):
    """Implements Smooth-L1 loss.
    y_true and y_pred are typicallly: [N, 4], but could be any shape.
    """
    diff = tf.abs(y_true - y_pred)
    less_than_one = tf.cast(tf.less(diff, 1.0), "float32")
    loss = (less_than_one * 0.5 * diff ** 2) + (1 - less_than_one) * (diff - 0.5)
    return loss
def log_sum(x):
    mx = tf.reduce_max(x)
    data = tf.log(tf.reduce_sum(tf.exp(x - mx), axis=1)) + mx
    return tf.reshape(data, (-1, 1))


def build_fpn_mask_graph(rois, feature_maps,cfg):

    ind = tf.zeros(shape=(tf.shape(rois)[0]),dtype=tf.int32)
    x = tf.image.crop_and_resize(feature_maps,rois,ind,crop_size=[14,14])


    x = slim.repeat(x,4,slim.conv2d,256,3)
    x = slim.conv2d_transpose(x,256,kernel_size=2,stride=2,activation_fn=slim.nn.relu)
    x = slim.conv2d(x,cfg.Config['num_classes'],1,1,activation_fn=slim.nn.sigmoid)
    return x


def mrcnn_mask_loss(target_masks, pred_masks):
    pred_masks = tf.transpose(pred_masks, [0, 3, 1, 2])
    loss = tf.keras.backend.switch(tf.size(target_masks) > 0,
                    tf.nn.sigmoid_cross_entropy_with_logits(labels=target_masks,logits=pred_masks),
                    tf.constant(0.0))
    loss = tf.reduce_mean(loss)
    return loss

def get_loss(conf_t,loc_t,pred_loc, pred_confs,cfg):

    anchor = utils.get_prio_box(cfg=cfg.Config)
    anchor = tf.tile(anchor,multiples=[cfg.batch_size,1])

   # conf_t, loc_t = utils.batch_slice(inputs=[truth_box, gt_lables], graph_fn=loss_clc, batch_size=batch_size)

    conf_t = tf.reshape(conf_t,shape=(-1,))
    loc_t = tf.reshape(loc_t,shape=(-1,4))

    positive_roi_ix = tf.where(conf_t > 0)[:, 0]

    positive_roi_class_ids = tf.cast(
        tf.gather(conf_t, positive_roi_ix), tf.int64)

    indices = tf.stack([positive_roi_ix, positive_roi_class_ids], axis=1)
    pred_loc = tf.reshape(pred_loc,shape=(-1,4))

    # Gather the deltas (predicted and true) that contribute to loss


    target_bbox = tf.gather(loc_t, positive_roi_ix)
    pred_bbox = tf.gather(pred_loc, positive_roi_ix)
    live_anchor = tf.gather(anchor, positive_roi_ix)

    decode_box = utils.decode_box(live_anchor,pred_bbox)
    x1,y1,x2,y2 = tf.split(decode_box,4,axis=1)

    crop_box = tf.concat([y1,x1,y2,x2],axis=1)




    target_bbox = tf.cast(target_bbox,tf.float32)
    loss = tf.keras.backend.switch(tf.cast(tf.size(target_bbox) > 0,tf.bool),
                                   smooth_l1_loss(y_true=target_bbox, y_pred=pred_bbox),
                                   tf.constant(0.0,dtype=tf.float32))
    loss_l = tf.reduce_sum(loss)

    pred_conf = tf.reshape(pred_confs,shape=(-1, cfg.Config['num_classes']))

    conf_t_tm = tf.cast(conf_t,tf.int32)
    conf_t_tm  = tf.reshape(conf_t_tm ,shape=(-1,))

    index = tf.stack([tf.range(tf.shape(conf_t_tm)[0]),conf_t_tm],axis=1)


    loss_c = log_sum(pred_conf) - tf.expand_dims(tf.gather_nd(pred_conf, index),-1)

    loss_c = tf.reshape(loss_c,shape=(cfg.batch_size,-1))
    conf_t = tf.reshape(conf_t, shape=(cfg.batch_size, -1))

    zeros = tf.zeros(shape=tf.shape(loss_c),dtype=tf.float32)
    loss_c = tf.where(tf.greater(conf_t,0),zeros,loss_c)


    pos_num = tf.reduce_sum(tf.cast(tf.greater(conf_t,0),dtype=tf.int32),axis=1)


    ne_num = pos_num*3

    los = []


    for s in range(cfg.batch_size):
        loss_tt = loss_c[s,:]
        value,ne_index = tf.nn.top_k(loss_tt,k=ne_num[s])
        pos_ix = tf.where(conf_t[s,:] > 0)[:,0]

        pos_ix = tf.cast(pos_ix,tf.int32)

        ix = tf.concat([pos_ix,ne_index],axis=0)


        label = tf.gather(conf_t[s,:],ix)
        label = tf.cast(label,tf.int32)
        label = tf.one_hot(label,depth=cfg.Config['num_classes'])
        logits = tf.gather(pred_confs[s,:],ix)



        ls = tf.keras.backend.switch(tf.cast(tf.size(label) > 0,tf.bool),
                                     tf.nn.softmax_cross_entropy_with_logits(labels=label, logits=logits),
                                 tf.constant(0.0))

        ls = tf.reduce_sum(ls)

        los.append(ls)

    num = tf.reduce_sum(pos_num)
    num = tf.cast(num,dtype=tf.float32)

    final_loss_c = tf.keras.backend.switch(num > 0,
                                           tf.reduce_sum(los) / num,
                                 tf.constant(0.0))

    final_loss_l = tf.keras.backend.switch(num > 0,
                                           loss_l / num,
                                           tf.constant(0.0))
    final_loss_c = final_loss_c

    tf.losses.add_loss(final_loss_c)


    tf.losses.add_loss(final_loss_l)

    total_loss = tf.losses.get_losses()
    tf.summary.scalar(name='class_loss',tensor=final_loss_c)
    tf.summary.scalar(name='loc_loss', tensor=final_loss_l)


    train_tensors = tf.identity(total_loss, 'ss')


    return train_tensors



def predict(ig,pred_loc, pred_confs, vbs,cfg):

    priors = utils.get_prio_box(cfg=cfg)

    box = utils.decode_box(prios=priors, pred_loc=pred_loc[0])
    props = slim.nn.softmax(pred_confs[0])
    pp = props[:,1:]

    cls = tf.argmax(pp, axis=1)
    pp = tf.reduce_max(pp,axis=1)


    ix = tf.where(tf.greater(pp,0.1))[:,0]
    score = tf.gather(pp,ix)
    box = tf.gather(box,ix)
    cls = tf.gather(cls, ix)
    box = tf.clip_by_value(box,clip_value_min=0.0,clip_value_max=1.0)

    keep = tf.image.non_max_suppression(
        scores=score,
        boxes=box,
        iou_threshold=0.5,
        max_output_size=50
    )
    return tf.gather(box,keep),tf.gather(score,keep),tf.gather(cls,keep)



def eger(cfg):
    tf.enable_eager_execution()

    gen = data_gen.get_batch(batch_size=cfg.batch_size,image_size=512)


    images, true_box, true_label = next(gen)

    loct, conft = np_utils.get_loc_conf(true_box, true_label, batch_size=cfg.batch_size,cfg=cfg.Config)


    pred_loc, pred_confs, mask,vbs = mask_iv2.inception_v2_ssd(images, config)


    get_loss(conft, loct, pred_loc, pred_confs, config)
import config
eger(config)