'''
Author:Kiru Park (park@acin.tuwien.ac.at, kirumang@gmail.com)
'''

import os,sys
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
import numpy as np
import yaml
import cv2
from matplotlib import pyplot as plt

#selection of detection pipelines
import keras
import tensorflow as tf

ROOT_DIR = os.path.abspath(".")
sys.path.append(ROOT_DIR)  # To find local version of the library
sys.path.append("./bop_toolkit")

from bop_toolkit_lib import inout
from tools import bop_io

cfg_path_detection = "ros_kinetic/ros_config.json"
cfg = inout.load_json(cfg_path_detection)

detect_type = cfg['detection_pipeline']
if detect_type=='rcnn':
    detection_dir=cfg['path_to_detection_pipeline']
    sys.path.append(detection_dir)
    from mrcnn.config import Config
    from mrcnn import utils
    import mrcnn.model as modellib
    from tools.mask_rcnn_util import BopInferenceConfig
    
#"/hsrb/head_rgbd_sensor/rgb/image_rect_color",
icp=False
if(icp):
    from rendering import utils as renderutil
    from rendering.renderer import Renderer
    from rendering.model import Model3D

from pix2pose_model import recognition as recog
import argparse
import time
import transforms3d as tf3d

import rospy
import ros_numpy
from sensor_msgs.msg import PointCloud2 as pc2
from sensor_msgs.msg import Image as ros_image
from geometry_msgs.msg import Pose

class pix2pose():
    def __init__(self,cfg):
        self.cfg=cfg
        self.rgb_topic = cfg['rgb_topic'] 
        self.depth_topic = cfg['depth_topic']
        self.camK = np.array(cfg['cam_K']).reshape(3,3)
        self.im_width = int(cfg['im_width'])
        self.im_height = int(cfg['im_height'])
        self.inlier_th = float(cfg['inlier_th'])
        self.ransac_th = float(cfg['ransac_th'])

        self.model_params =  inout.load_json(cfg['norm_factor_fn'])
        self.detection_labels= cfg['obj_labels'] #labels of corresponding detections
        if(icp):
         self.ren = Renderer((self.im_width, self.im_height), self.camK)

        n_objs  = int(cfg['n_objs'])
        self.target_objs = cfg['target_obj_name']
        self.colors= np.random.randint(0,255,(n_objs,3))
        if(detect_type=="rcnn"):
            #Load mask r_cnn
            '''
            standard estimation parameter for Mask R-CNN (identical for all dataset)
            '''
            self.config = BopInferenceConfig(dataset="ros",
                                    num_classes=n_objs+1,
                                    im_width=self.im_width,im_height=self.im_height)            
            self.config.DETECTION_MIN_CONFIDENCE=0.3
            self.config.DETECTION_MAX_INSTANCES=30
            self.config.DETECTION_NMS_THRESHOLD=0.5
            
            self.detection_model = modellib.MaskRCNN(mode="inference", config=self.config,model_dir="/")
            self.detection_model.load_weights(cfg['path_to_detection_weights'], by_name=True)

        self.obj_models=[]
        self.obj_bboxes=[]

        self.obj_pix2pose=[]
        pix2pose_dir = cfg['path_to_pix2pose_weights']
        th_outlier = cfg['outlier_th']
        self.model_scale = cfg['model_scale']
        for t_id,target_obj in enumerate(self.target_objs):
            weight_fn = os.path.join(pix2pose_dir,"{:02d}/inference.hdf5".format(target_obj))
            print("Load pix2pose weights from ",weight_fn)
            model_param = self.model_params['{}'.format(target_obj)]
            obj_param=bop_io.get_model_params(model_param)
            recog_temp = recog.pix2pose(weight_fn,camK= self.camK,
                                       res_x=self.im_width,res_y=self.im_height,obj_param=obj_param,
                                       th_ransac=self.ransac_th,th_outlier=th_outlier,th_inlier=self.inlier_th)
            self.obj_pix2pose.append(recog_temp)
            ply_fn = os.path.join(self.cfg['model_dir'],self.cfg['ply_files'][t_id])               
            if(icp):
               obj_model = Model3D()
               obj_model.load(ply_fn, scale=cfg['model_scale'])
               self.obj_models.append(obj_model)
            else:
               obj_model = inout.load_ply(ply_fn)
               self.obj_bboxes.append(self.get_3d_box_points(obj_model['pts']))

        rospy.init_node('pix2pose', anonymous=True)
        self.detect_pub = rospy.Publisher("/pix2pose/detected_object",ros_image)
        
        #self.pose_pub = rospy.Publisher("/pix2pose/object_pose", Pose)
        self.pose_pub = rospy.Publisher("/pix2pose/object_pose", ros_image)
        self.sub = rospy.Subscriber(self.rgb_topic, ros_image, self.callback)
        self.graph = tf.get_default_graph()
    def get_3d_box_points(self,vertices):
        x_min = np.min(vertices[:,0])
        y_min = np.min(vertices[:,1])
        z_min = np.min(vertices[:,2])
        x_max = np.max(vertices[:,0])
        y_max = np.max(vertices[:,1])
        z_max = np.max(vertices[:,2])
        pts=[]
        pts.append([x_min,y_min,z_min])#0
        pts.append([x_min,y_min,z_max])#1        
        pts.append([x_min,y_max,z_min])#2
        pts.append([x_min,y_max,z_max])#3        
        pts.append([x_max,y_min,z_min])#4
        pts.append([x_max,y_min,z_max])#5                
        pts.append([x_max,y_max,z_min])#6
        pts.append([x_max,y_max,z_max])#7
        return np.array(pts)*self.model_scale
    def draw_3d_poses(self,obj_box,tf,img):
        lines=[[0,1],[0,2],[0,4],[1,5],[1,3],[2,6],[2,3],[3,7],
               [4,6],[4,5],[5,7],[6,7]]   
        direc= [2,1,0,0,1,0,2,0,1,2,1,2]
        proj_2d = np.zeros((8,2),dtype=np.int)
        tf_pts = (np.matmul(tf[:3,:3],obj_box.T)+tf[:3,3,np.newaxis]).T
        max_z = np.max(tf_pts[:,2])
        min_z = np.min(tf_pts[:,2])
        z_diff = max_z-min_z
        z_mean = (max_z+min_z)/2
        proj_2d[:,0] = tf_pts[:,0]/tf_pts[:,2]*self.camK[0,0]+self.camK[0,2]
        proj_2d[:,1] = tf_pts[:,1]/tf_pts[:,2]*self.camK[1,1]+self.camK[1,2]    
        for l_id in range(len(lines)):        
            line = lines[l_id]
            dr= direc[l_id]
            mean_z_line =( tf_pts[line[0],2] +tf_pts[line[1],2])/2
            color_amp = (z_mean-mean_z_line)/z_diff*255
            color = np.zeros((3),dtype=np.uint8)
            color[dr] = min(128+color_amp,255)
            if(color[dr]<10):
                continue
            cv2.line(img,(proj_2d[line[0],0],proj_2d[line[0],1]),
                     (proj_2d[line[1],0],proj_2d[line[1],1]),
                     (int(color[0]),int(color[1]),int(color[2])),2)
        
        pt_colors=[[255,255,255],[255,0,0],[0,255,0],[0,0,255]]
        for pt_id,color in zip([0,4,2,1],pt_colors): #origin, x,y,z, points
            pt =proj_2d [pt_id]
            cv2.circle(img,(int(pt[0]),int(pt[1])),1,(color[0],color[1],color[2]),5)
        return img
    
    def get_rcnn_detection(self,image_t):
        image_t_resized, window, scale, padding, crop = utils.resize_image(
                        np.copy(image_t),
                        min_dim=self.config.IMAGE_MIN_DIM,
                        min_scale=self.config.IMAGE_MIN_SCALE,
                        max_dim=self.config.IMAGE_MAX_DIM,
                        mode=self.config.IMAGE_RESIZE_MODE)
        if(scale!=1):
            print("Warning.. have to adjust the scale")        
        results = self.detection_model.detect([image_t_resized], verbose=0)
        r = results[0]
        rois = r['rois']
        rois = rois - [window[0],window[1],window[0],window[1]]
        obj_orders = np.array(r['class_ids'])-1
        obj_ids=[]
        for obj_order in obj_orders:
            obj_ids.append(self.detection_labels[obj_order])
        #now c_ids are the same annotation those of the names of ply/gt files
        scores = np.array(r['scores'])
        masks = r['masks'][window[0]:window[2],window[1]:window[3],:]
        return rois,obj_orders,obj_ids,scores,masks

    def run(self):
        rospy.spin()

    def callback(self,r_image):        
        with self.graph.as_default():
            data = ros_numpy.numpify(r_image)        
            image=np.copy(data) #bgr -> rgb order
            bbox_pred = np.zeros((4),np.int)
            rois,obj_orders,obj_ids,scores,masks= self.get_rcnn_detection(image)
            result_scores=[]
            result_poses=[]
            result_ids=[]
            result_bbox=[]
            img_detection = np.copy(image)
            img_pose=np.copy(image)
            for r_id,roi in enumerate(rois):
                  if(roi[0]==-1 and roi[1]==-1):
                        continue
                  obj_id = obj_ids[r_id] 

                  if(detect_type=='rcnn'):       
                        mask_from_detect = masks[:,:,r_id]   
                        r_=int(self.colors[obj_orders[r_id],0])
                        g_=int(self.colors[obj_orders[r_id],1])
                        b_=int(self.colors[obj_orders[r_id],2])
                        img_detection[mask_from_detect,0]=np.minimum(255,img_detection[mask_from_detect,0]+0.8*r_)
                        img_detection[mask_from_detect,1]=np.minimum(255,img_detection[mask_from_detect,1]+0.8*g_)
                        img_detection[mask_from_detect,2]=np.minimum(255,img_detection[mask_from_detect,2]+0.8*b_)
                        cv2.rectangle(img_detection,(roi[1],roi[0]),(roi[3],roi[2]),(r_,g_,b_),2)
                        cv2.putText(img_detection,'{}'.format(obj_id),(roi[1],roi[0]),cv2.FONT_HERSHEY_SIMPLEX,1,(255,255,255),2,1)

                  if not(obj_id in self.target_objs):
                        continue           
                  
                  
                  pix2pose_id= self.target_objs.index(obj_id)
                  _,mask_pred,rot_pred,tra_pred,frac_inlier,_ =\
                  self.obj_pix2pose[pix2pose_id].est_pose(image,roi.astype(np.int))            
                  if(frac_inlier==-1):
                        continue        
                  if(detect_type=='rcnn'):                               
                        union = np.sum(np.logical_or(mask_from_detect,mask_pred))
                        if(union==0):
                           mask_iou=0
                           score = 0
                        else:
                           mask_iou = np.sum(np.logical_and(mask_from_detect,mask_pred))/union
                           score=scores[r_id]*frac_inlier*mask_iou*1000
                  else:
                        score = scores[r_id]
                  result_scores.append(score)
                  tra_pred = tra_pred*self.model_scale #mm to m
                  if(tra_pred[2]<0.1 or tra_pred[2]>5):
                      continue
                  pred_tf=np.eye(4)
                  pred_tf[:3,:3]=rot_pred
                  pred_tf[:3,3]=tra_pred
                  result_poses.append(pred_tf)
                  result_ids.append(pix2pose_id)
                  result_bbox.append(roi)
            #currently publish pose of the top scored objects
            self.detect_pub.publish(ros_numpy.msgify(ros_image, img_detection[:,:,::-1], encoding='bgr8')) 
            #render detection results
            #render pose estimation results      
            for o_id, tf,score,roi in zip(result_ids,result_poses,result_scores,result_bbox):
                img_pose = self.draw_3d_poses(self.obj_bboxes[o_id],tf,img_pose)
                cv2.putText(img_pose,'{:.3f}'.format(score),(roi[1],roi[0]),cv2.FONT_HERSHEY_SIMPLEX,1,(255,255,255),2,1)
            self.pose_pub.publish(ros_numpy.msgify(ros_image, img_pose[:,:,::-1], encoding='bgr8'))                  
if __name__ == '__main__':
    r= pix2pose(cfg)
    r.run()

	






	
