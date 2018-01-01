# -*- coding: utf-8 -*-
import tensorflow as tf
import numpy as np
np.random.seed(111)
from keras.layers import Input

BOX_IDX_X = 0
BOX_IDX_Y = 1
BOX_IDX_W = 2
BOX_IDX_H = 3
BOX_IDX_CONFIDENCE = 4
BOX_IDX_CLASS_START = 5


class YoloLoss(object):
    
    def __init__(self,
                 true_boxes=Input(shape=(1, 1, 1, 10 , 4)),
                 grid_size=13,
                 nb_class=1,
                 anchors=[0.57273, 0.677385, 1.87446, 2.06253, 3.33843, 5.47434, 7.88282, 3.52778, 9.77052, 9.16828]):
        """
        # Args
            grid_size : int
            batch_size : int
            anchors : list of floats
            nb_box : int
            nb_class : int
            true_boxes : Tensor instance
        """
        self.grid_size = grid_size
        self.anchors = anchors
        self.nb_box = int(len(anchors)/2)
        self.nb_class = nb_class
        self.true_boxes = true_boxes
        
        self.coord_scale = 1.0

        # Todo : create method를 따로 만들어서 주입받자.
        self._activator = _Activator(self.anchors)
        self._mask = _Mask(nb_class)


    def custom_loss(self, batch_size, warmup_bs):
        """
        # Args
            y_true : (N, 13, 13, 5, 6)
            y_pred : (N, 13, 13, 5, 6)
        
        """
        def loss_func(y_true, y_pred):
            # 1. activate prediction & truth tensor
            # Todo : return tensor를 2개로 줄이자.
            pred_box_xy, pred_box_wh, pred_box_conf, pred_box_class, true_box_xy, true_box_wh, true_box_conf, true_box_class = self._activator.run(y_true, y_pred)
            
            # 2. mask
            coord_mask = self._mask.create_coord_mask(y_true)
            class_mask = self._mask.create_class_mask(y_true, true_box_class)
            conf_mask = self._mask.create_conf_mask(y_true, self.true_boxes, pred_box_xy, pred_box_wh)
            
            """
            Warm-up training
            """
            no_boxes_mask = tf.to_float(coord_mask < self.coord_scale/2.)
            seen = tf.assign_add(tf.Variable(0.), 1.)
            cell_grid = create_cell_grid(tf.shape(y_pred)[1], batch_size)
            true_box_xy, true_box_wh, coord_mask = tf.cond(tf.less(seen, warmup_bs), 
                                  lambda: [true_box_xy + (0.5 + cell_grid) * no_boxes_mask, 
                                           true_box_wh + tf.ones_like(true_box_wh) * np.reshape(self.anchors, [1,1,1,self.nb_box,2]) * no_boxes_mask, 
                                           tf.ones_like(coord_mask)],
                                  lambda: [true_box_xy, 
                                           true_box_wh,
                                           coord_mask])
            
            """
            Finalize the loss
            """
            nb_coord_box = tf.reduce_sum(tf.to_float(coord_mask > 0.0))
            nb_conf_box  = tf.reduce_sum(tf.to_float(conf_mask  > 0.0))
            nb_class_box = tf.reduce_sum(tf.to_float(class_mask > 0.0))
            
            loss_xy    = tf.reduce_sum(tf.square(true_box_xy-pred_box_xy)     * coord_mask) / (nb_coord_box + 1e-6) / 2.
            loss_wh    = tf.reduce_sum(tf.square(true_box_wh-pred_box_wh)     * coord_mask) / (nb_coord_box + 1e-6) / 2.
            loss_conf  = tf.reduce_sum(tf.square(true_box_conf-pred_box_conf) * conf_mask)  / (nb_conf_box  + 1e-6) / 2.
            loss_class = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=true_box_class, logits=pred_box_class)
            loss_class = tf.reduce_sum(loss_class * class_mask) / (nb_class_box + 1e-6)
            
            loss = loss_xy + loss_wh + loss_conf + loss_class
            
            # [1 13 13 5]
            true_box_class = tf.Print(true_box_class,[tf.shape(true_box_class)], message="tensor shape: ", summarize=1000)
            for i in range(13):
                for j in range(13):
                    loss = tf.Print(loss, [true_box_class[0, i, j, :]], message="true box class {}, {}".format(i, j), summarize=1000)
            
            return loss
        return loss_func


class _Activator(object):
    
    def __init__(self, anchors):
        self._anchor_boxes = np.reshape(anchors, [1,1,1,-1,2])
        
    def run(self, y_true, y_pred):
        pred_box_xy, pred_box_wh, pred_box_conf, pred_box_class = self._activate_pred_tensor(y_pred)
        true_box_xy, true_box_wh, true_box_conf, true_box_class = self._activate_true_tensor(y_true, pred_box_xy, pred_box_wh)
        # Todo : many returns
        return pred_box_xy, pred_box_wh, pred_box_conf, pred_box_class, true_box_xy, true_box_wh, true_box_conf, true_box_class
    
    def _activate_pred_tensor(self, y_pred):
        """
        # Args
            y_pred : (N, 13, 13, 5, 6)
            cell_grid : (N, 13, 13, 5, 2)
        
        # Returns
            box_xy : (N, 13, 13, 5, 2)
                1) sigmoid activation
                2) grid offset added
            box_wh : (N, 13, 13, 5, 2)
                1) exponential activation
                2) anchor box multiplied
            box_conf : (N, 13, 13, 5, 1)
                1) sigmoid activation
            box_classes : (N, 13, 13, 5, nb_class)
        """
        # bx = sigmoid(tx) + cx, by = sigmoid(ty) + cy
        batch_size = tf.shape(y_pred)[0]
        grid_size = tf.shape(y_pred)[1]
        cell_grid = create_cell_grid(grid_size, batch_size)
        
        pred_box_xy = tf.sigmoid(y_pred[..., :2]) + cell_grid
        pred_box_wh = tf.exp(y_pred[..., 2:4]) * self._anchor_boxes
        pred_box_conf = tf.sigmoid(y_pred[..., 4])
        pred_box_class = y_pred[..., 5:]
        return pred_box_xy, pred_box_wh, pred_box_conf, pred_box_class

    def _activate_true_tensor(self, y_true, pred_box_xy, pred_box_wh):
        ### adjust x and y
        true_box_xy = y_true[..., 0:2] # relative position to the containing cell
        
        ### adjust w and h
        true_box_wh = y_true[..., 2:4] # number of cells accross, horizontally and vertically
        
        ### adjust confidence
        true_wh_half = true_box_wh / 2.
        true_mins    = true_box_xy - true_wh_half
        true_maxes   = true_box_xy + true_wh_half
        
        pred_wh_half = pred_box_wh / 2.
        pred_mins    = pred_box_xy - pred_wh_half
        pred_maxes   = pred_box_xy + pred_wh_half       
        
        intersect_mins  = tf.maximum(pred_mins,  true_mins)
        intersect_maxes = tf.minimum(pred_maxes, true_maxes)
        intersect_wh    = tf.maximum(intersect_maxes - intersect_mins, 0.)
        intersect_areas = intersect_wh[..., 0] * intersect_wh[..., 1]
        
        true_areas = true_box_wh[..., 0] * true_box_wh[..., 1]
        pred_areas = pred_box_wh[..., 0] * pred_box_wh[..., 1]
    
        union_areas = pred_areas + true_areas - intersect_areas
        iou_scores  = tf.truediv(intersect_areas, union_areas)
        
        true_box_conf = iou_scores * y_true[..., 4]
        
        ### adjust class probabilities
        true_box_class = tf.argmax(y_true[..., 5:], -1)
        
        return true_box_xy, true_box_wh, true_box_conf, true_box_class


def create_cell_grid(grid_size, batch_size):
    cell_x = tf.to_float(tf.reshape(tf.tile(tf.range(grid_size), [grid_size]), (1, grid_size, grid_size, 1, 1)))
    cell_y = tf.transpose(cell_x, (0,2,1,3,4))
    cell_grid = tf.tile(tf.concat([cell_x, cell_y], -1), [batch_size, 1, 1, 5, 1])
    return cell_grid


class _Mask(object):
    
    def __init__(self, nb_class=1, coord_scale=1.0, class_scale=1.0, object_scale=5.0, no_object_scale=1.0):
        self._nb_class = nb_class
        self._coord_scale = coord_scale
        self._class_scale = class_scale
        self._object_scale = object_scale
        self._no_object_scale = no_object_scale
        
    def create_coord_mask(self, y_true):
        # 1) coordinate mask: simply the position of the ground truth boxes (the predictors)
        #     BOX 별 confidence value 를 mask value 로 사용
        # [1 13 13 5 1]
        mask = tf.expand_dims(y_true[..., BOX_IDX_CONFIDENCE], axis=-1) * self._coord_scale
        return mask
    
    def create_class_mask(self, y_true, true_box_class):
        # 2) class mask: simply the position of the ground truth boxes (the predictors)
        class_wt = np.ones(self._nb_class, dtype='float32')
        mask = y_true[..., 4] * tf.gather(class_wt, true_box_class) * self._class_scale
        return mask
    
    def create_conf_mask(self, y_true, true_boxes, pred_box_xy, pred_box_wh):
        ### confidence mask: penelize predictors + penalize boxes with low IOU
        # penalize the confidence of the boxes, which have IOU with some ground truth box < 0.6
        true_xy = true_boxes[..., 0:2]
        true_wh = true_boxes[..., 2:4]
        
        true_wh_half = true_wh / 2.
        true_mins    = true_xy - true_wh_half
        true_maxes   = true_xy + true_wh_half
        
        pred_xy = tf.expand_dims(pred_box_xy, 4)
        pred_wh = tf.expand_dims(pred_box_wh, 4)
        
        pred_wh_half = pred_wh / 2.
        pred_mins    = pred_xy - pred_wh_half
        pred_maxes   = pred_xy + pred_wh_half    
        
        intersect_mins  = tf.maximum(pred_mins,  true_mins)
        intersect_maxes = tf.minimum(pred_maxes, true_maxes)
        intersect_wh    = tf.maximum(intersect_maxes - intersect_mins, 0.)
        intersect_areas = intersect_wh[..., 0] * intersect_wh[..., 1]
        
        true_areas = true_wh[..., 0] * true_wh[..., 1]
        pred_areas = pred_wh[..., 0] * pred_wh[..., 1]

        union_areas = pred_areas + true_areas - intersect_areas
        iou_scores  = tf.truediv(intersect_areas, union_areas)

        best_ious = tf.reduce_max(iou_scores, axis=4)
        # 1) confidence mask (N, 13, 13, 5)
        conf_mask  = tf.zeros(tf.shape(y_true)[:4])
        conf_mask = conf_mask + tf.to_float(best_ious < 0.6) * (1 - y_true[..., 4]) * self._no_object_scale
        
        # penalize the confidence of the boxes, which are reponsible for corresponding ground truth box
        conf_mask = conf_mask + y_true[..., 4] * self._object_scale
        return conf_mask


import pytest
@pytest.fixture(scope='function')
def setup_y_true_tensor(request):
    y_true = tf.placeholder(tf.float32, [None, 13, 13, 5, 6], name='y_true')
    y_true_value = np.zeros((1,13,13,5,6))
    y_true_value[0,7,6,4,:] = [6.015625, 7.71875, 8.84375, 10, 1, 1]    # (cx, cy, w, h, confidence, classes)
    return y_true, y_true_value


def test_yolo_coord_masking(setup_y_true_tensor):
    # 1. setup y_true placeholder
    # 2. setup y_true feed value
    y_true, y_true_value = setup_y_true_tensor
    
    # 3. create coord_mask operation
    yolo_mask = _Mask(nb_class=1, coord_scale=1.0, class_scale=1.0, object_scale=5.0, no_object_scale=1.0)
    coord_mask_op = yolo_mask.create_coord_mask(y_true)

    # 4. run loss_op in session
    sess = tf.Session()
    init_op = tf.global_variables_initializer()
    sess.run(init_op)
    coord_mask_value = sess.run(coord_mask_op, feed_dict={y_true: y_true_value})
    sess.close()
    
    # coordinate mask value : (N, grid, grid, nb_box, 1)
    #     object 가 있는 (grid_x, grid_y, anchor_idx) 에만 1, 나머지는 0
    expected_coord_mask = np.zeros((1,13,13,5,1))
    expected_coord_mask[0, 7, 6, 4, :] = 1.0
    assert np.allclose(coord_mask_value, expected_coord_mask)



def test_loss_op(setup_y_true_tensor):
    # 1. build loss function
    batch_size = 1
    warmup_bs = 0
    yolo_loss = YoloLoss()
    custom_loss = yolo_loss.custom_loss(batch_size, warmup_bs)

    # 2. placeholder : (y_true, y_pred)
    y_true, y_true_value = setup_y_true_tensor
    y_pred = tf.placeholder(tf.float32, [None, 13, 13, 5, 6], name='y_pred')

    # 3. loss operation
    loss_op = custom_loss(y_true, y_pred)
    
    # 4. setup feed values for each placeholders (true_boxes, y_true, y_pred
    y_pred_value = np.random.randn(1,13,13,5,6) / 4
    true_boxes_value = np.zeros((1,1,1,1,10,4))
    true_boxes_value[0,0,0,0,0,:] = [6.015625, 7.71875, 8.84375, 10]
    
    # 5. run loss_op in session
    # y_true, y_pred에 실제 value를 insert
    sess = tf.Session()
    init_op = tf.global_variables_initializer()
    sess.run(init_op)
    loss_value = sess.run(loss_op, feed_dict={yolo_loss.true_boxes: true_boxes_value,
                                              y_true: y_true_value,
                                              y_pred: y_pred_value})
    sess.close()
    assert np.allclose(loss_value, 11.471475)
    

import pytest
if __name__ == '__main__':
    pytest.main([__file__, "-v", "-s"])


