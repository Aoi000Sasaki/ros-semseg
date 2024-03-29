from __future__ import absolute_import
from __future__ import division

import argparse
import os
import torch
import cv2
import numpy as np
import rospy
from sensor_msgs.msg import Image as ROS_Image
from cv_bridge import CvBridge

from apex import amp
from runx.logx import logx
from config import assert_and_infer_cfg, cfg
from utils.misc import AverageMeter, prep_experiment
from utils.trnval_utils import eval_minibatch
from loss.utils import get_loss
from loss.optimizer import get_optimizer, restore_net

import datasets
import network


# for debug
from PIL import Image


parser = argparse.ArgumentParser(description='Semantic Segmentation')
parser.add_argument('--lr', type=float, default=0.002)
parser.add_argument('--arch', type=str, default='ocrnet.HRNet_Mscale',
                    help='Network architecture. We have DeepSRNX50V3PlusD (backbone: ResNeXt50) \
                    and deepWV3Plus (backbone: WideResNet38).')
parser.add_argument('--dataset', type=str, default='cityscapes',
                    help='cityscapes, mapillary, camvid, kitti')
parser.add_argument('--dataset_inst', default=None,
                    help='placeholder for dataset instance')
parser.add_argument('--num_workers', type=int, default=4,
                    help='cpu worker threads per dataloader instance')

parser.add_argument('--cv', type=int, default=0,
                    help=('Cross-validation split id to use. Default # of splits set'
                          ' to 3 in config'))

parser.add_argument('--class_uniform_pct', type=float, default=0.5,
                    help='What fraction of images is uniformly sampled')
parser.add_argument('--class_uniform_tile', type=int, default=1024,
                    help='tile size for class uniform sampling')
parser.add_argument('--coarse_boost_classes', type=str, default=None,
                    help='Use coarse annotations for specific classes')

parser.add_argument('--custom_coarse_dropout_classes', type=str, default=None,
                    help='Drop some classes from auto-labelling')

parser.add_argument('--img_wt_loss', action='store_true', default=False,
                    help='per-image class-weighted loss')
parser.add_argument('--rmi_loss', action='store_true', default=False,
                    help='use RMI loss')
parser.add_argument('--batch_weighting', action='store_true', default=False,
                    help=('Batch weighting for class (use nll class weighting using '
                          'batch stats'))

parser.add_argument('--jointwtborder', action='store_true', default=False,
                    help='Enable boundary label relaxation')
parser.add_argument('--strict_bdr_cls', type=str, default='',
                    help='Enable boundary label relaxation for specific classes')
parser.add_argument('--rlx_off_epoch', type=int, default=-1,
                    help='Turn off border relaxation after specific epoch count')
parser.add_argument('--rescale', type=float, default=1.0,
                    help='Warm Restarts new lr ratio compared to original lr')
parser.add_argument('--repoly', type=float, default=1.5,
                    help='Warm Restart new poly exp')

parser.add_argument('--apex', action='store_true', default=True,
                    help='Use Nvidia Apex Distributed Data Parallel')
parser.add_argument('--fp16', action='store_true', default=True,
                    help='Use Nvidia Apex AMP')

parser.add_argument('--local_rank', default=0, type=int,
                    help='parameter used by apex library')
parser.add_argument('--global_rank', default=0, type=int,
                    help='parameter used by apex library')

parser.add_argument('--optimizer', type=str, default='sgd', help='optimizer')
parser.add_argument('--amsgrad', action='store_true', help='amsgrad for adam')

parser.add_argument('--freeze_trunk', action='store_true', default=False)
parser.add_argument('--hardnm', default=0, type=int,
                    help=('0 means no aug, 1 means hard negative mining '
                          'iter 1, 2 means hard negative mining iter 2'))

parser.add_argument('--trunk', type=str, default='resnet101',
                    help='trunk model, can be: resnet101 (default), resnet50')
parser.add_argument('--max_epoch', type=int, default=180)
parser.add_argument('--max_cu_epoch', type=int, default=150,
                    help='Class Uniform Max Epochs')
parser.add_argument('--start_epoch', type=int, default=0)
parser.add_argument('--color_aug', type=float,
                    default=0.25, help='level of color augmentation')
parser.add_argument('--gblur', action='store_true', default=False,
                    help='Use Guassian Blur Augmentation')
parser.add_argument('--bblur', action='store_true', default=False,
                    help='Use Bilateral Blur Augmentation')
parser.add_argument('--brt_aug', action='store_true', default=False,
                    help='Use brightness augmentation')
parser.add_argument('--lr_schedule', type=str, default='poly',
                    help='name of lr schedule: poly')
parser.add_argument('--poly_exp', type=float, default=1.0,
                    help='polynomial LR exponent')
parser.add_argument('--poly_step', type=int, default=110,
                    help='polynomial epoch step')
parser.add_argument('--bs_trn', type=int, default=2,
                    help='Batch size for training per gpu')
parser.add_argument('--bs_val', type=int, default=1,
                    help='Batch size for Validation per gpu')
parser.add_argument('--crop_size', type=str, default='896',
                    help=('training crop size: either scalar or h,w'))
parser.add_argument('--scale_min', type=float, default=0.5,
                    help='dynamically scale training images down to this size')
parser.add_argument('--scale_max', type=float, default=2.0,
                    help='dynamically scale training images up to this size')
parser.add_argument('--weight_decay', type=float, default=1e-4)
parser.add_argument('--momentum', type=float, default=0.9)
parser.add_argument('--snapshot', type=str, default='ASSETS_PATH/seg_weights/cityscapes_ocrnet.HRNet_Mscale_outstanding-turtle.pth')
parser.add_argument('--resume', type=str, default=None,
                    help=('continue training from a checkpoint. weights, '
                          'optimizer, schedule are restored'))
parser.add_argument('--restore_optimizer', action='store_true', default=False)
parser.add_argument('--restore_net', action='store_true', default=False)
parser.add_argument('--exp', type=str, default='default',
                    help='experiment directory name')
parser.add_argument('--result_dir', type=str, default='/home/user/ws/src/semantic-segmentation/testresult/1229',
                    help='where to write log output')
parser.add_argument('--syncbn', action='store_true', default=True,
                    help='Use Synchronized BN')
parser.add_argument('--dump_augmentation_images', action='store_true', default=False,
                    help='Dump Augmentated Images for sanity check')
parser.add_argument('--test_mode', action='store_true', default=False,
                    help=('Minimum testing to verify nothing failed, '
                          'Runs code for 1 epoch of train and val'))
parser.add_argument('-wb', '--wt_bound', type=float, default=1.0,
                    help='Weight Scaling for the losses')
parser.add_argument('--maxSkip', type=int, default=0,
                    help='Skip x number of  frames of video augmented dataset')
parser.add_argument('--scf', action='store_true', default=False,
                    help='scale correction factor')
# Full Crop Training
parser.add_argument('--full_crop_training', action='store_true', default=False,
                    help='Full Crop Training')

# Multi Scale Inference
parser.add_argument('--multi_scale_inference', action='store_true',
                    help='Run multi scale inference')

parser.add_argument('--default_scale', type=float, default=1.0,
                    help='default scale to run validation')

parser.add_argument('--log_msinf_to_tb', action='store_true', default=False,
                    help='Log multi-scale Inference to Tensorboard')

parser.add_argument('--eval', type=str, default='folder',
                    help=('just run evaluation, can be set to val or trn or '
                          'folder'))
parser.add_argument('--eval_folder', type=str, default='/home/user/ws/src/semantic-segmentation/testpic',
                    help='path to frames to evaluate')
parser.add_argument('--three_scale', action='store_true', default=False)
parser.add_argument('--alt_two_scale', action='store_true', default=False)
parser.add_argument('--do_flip', action='store_true', default=False)
parser.add_argument('--extra_scales', type=str, default='0.5,2.0')
parser.add_argument('--n_scales', type=str, default='0.5,1.0,2.0')
parser.add_argument('--align_corners', action='store_true',
                    default=False)
parser.add_argument('--translate_aug_fix', action='store_true', default=False)
parser.add_argument('--mscale_lo_scale', type=float, default=0.5,
                    help='low resolution training scale')
parser.add_argument('--pre_size', type=int, default=None,
                    help=('resize long edge of images to this before'
                          ' augmentation'))
parser.add_argument('--amp_opt_level', default='O1', type=str,
                    help=('amp optimization level'))
parser.add_argument('--rand_augment', default=None,
                    help='RandAugment setting: set to \'N,M\'')
parser.add_argument('--init_decoder', default=False, action='store_true',
                    help='initialize decoder with kaiming normal')
parser.add_argument('--dump_topn', type=int, default=0,
                    help='Dump worst val images')
parser.add_argument('--dump_assets', action='store_true',
                    help='Dump interesting assets')
parser.add_argument('--dump_all_images', action='store_true',
                    help='Dump all images, not just a subset')
parser.add_argument('--dump_for_submission', action='store_true',
                    help='Dump assets for submission')
parser.add_argument('--dump_for_auto_labelling', action='store_true',
                    help='Dump assets for autolabelling')
parser.add_argument('--dump_topn_all', action='store_true', default=False,
                    help='dump topN worst failures')
parser.add_argument('--custom_coarse_prob', type=float, default=None,
                    help='Custom Coarse Prob')
parser.add_argument('--only_coarse', action='store_true', default=False)
parser.add_argument('--mask_out_cityscapes', action='store_true',
                    default=False)
parser.add_argument('--ocr_aspp', action='store_true', default=False)
parser.add_argument('--map_crop_val', action='store_true', default=False)
parser.add_argument('--aspp_bot_ch', type=int, default=None)
parser.add_argument('--trial', type=int, default=None)
parser.add_argument('--mscale_cat_scale_flt', action='store_true',
                    default=False)
parser.add_argument('--mscale_dropout', action='store_true',
                    default=False)
parser.add_argument('--mscale_no3x3', action='store_true',
                    default=False, help='no inner 3x3')
parser.add_argument('--mscale_old_arch', action='store_true',
                    default=False, help='use old attention head')
parser.add_argument('--mscale_init', type=float, default=None,
                    help='default attention initialization')
parser.add_argument('--attnscale_bn_head', action='store_true',
                    default=False)
parser.add_argument('--set_cityscapes_root', type=str, default=None,
                    help='override cityscapes default root dir')
parser.add_argument('--ocr_alpha', type=float, default=None,
                    help='set HRNet OCR auxiliary loss weight')
parser.add_argument('--val_freq', type=int, default=1,
                    help='how often (in epochs) to run validation')
parser.add_argument('--deterministic', action='store_true',
                    default=False)
parser.add_argument('--summary', action='store_true',
                    default=False)
parser.add_argument('--segattn_bot_ch', type=int, default=None,
                    help='bottleneck channels for seg and attn heads')
parser.add_argument('--grad_ckpt', action='store_true',
                    default=False)
parser.add_argument('--no_metrics', action='store_true', default=False,
                    help='prevent calculation of metrics')
parser.add_argument('--supervised_mscale_loss_wt', type=float, default=None,
                    help='weighting for the supervised loss')
parser.add_argument('--ocr_aux_loss_rmi', action='store_true', default=False,
                    help='allow rmi for aux loss')


class SemanticSegmentation():
    def __init__(self):
        self.args = parser.parse_args()
        self.val_loader = None
        self.net = None
        self.dumper = None
        self.is_fst_img = True
        self.args.world_size = 1
        self.args.best_record = {'epoch': -1, 'iter': 0, 'val_loss': 1e10, 'acc': 0,
                    'acc_cls': 0, 'mean_iu': 0, 'fwavacc': 0}
        self.bridge = CvBridge()
        self.pub = rospy.Publisher('/pred_img', ROS_Image, queue_size=1)
        torch.backends.cudnn.benchmark = True

        if 'WORLD_SIZE' in os.environ and self.args.apex:
            self.args.world_size = int(os.environ['WORLD_SIZE'])
            self.args.global_rank = int(os.environ['RANK'])

        if self.args.apex:
            print('Global Rank: {} Local Rank: {}'.format(
                self.args.global_rank, self.args.local_rank))
            torch.cuda.set_device(self.args.local_rank)
            torch.distributed.init_process_group(backend='nccl',
                                                init_method='env://')
            
        logx.initialize(logdir=self.args.result_dir,
                tensorboard=True, hparams=vars(self.args),
                global_rank=self.args.global_rank)
        
        assert_and_infer_cfg(self.args)
        prep_experiment(self.args)
        _, self.val_loader, _ = datasets.setup_loaders(self.args)
        criterion, _ = get_loss(self.args)

        if 'ASSETS_PATH' in self.args.snapshot:
            self.args.snapshot = self.args.snapshot.replace('ASSETS_PATH', cfg.ASSETS_PATH)
        checkpoint = torch.load(self.args.snapshot,
                                map_location=torch.device('cpu'))
        msg = "Loading weights from: checkpoint={}".format(self.args.snapshot)
        logx.msg(msg)

        self.net = network.get_net(self.args, criterion)
        optim, _ = get_optimizer(self.args, self.net)

        if self.args.fp16:
            self.net, _ = amp.initialize(self.net, optim, opt_level=self.args.amp_opt_level)

        self.net = network.wrap_network_in_dataparallel(self.net, self.args.apex)

        restore_net(self.net, checkpoint)

        torch.cuda.empty_cache()

    def predict(self, msg):
        header_info = msg.header
        cv_img = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        tf_img = torch.from_numpy(cv_img)

        if self.is_fst_img == True:
            self.net.eval()
            self.is_fst_img = False
        val_loss = AverageMeter()

        data = next(iter(self.val_loader))
        input_images, _, _, _ = data

        tf_img = tf_img.unsqueeze(0)
        tf_img = tf_img.permute(0, 3, 1, 2)
        tf_img = tf_img / 255.0
        gt_img_shape = (input_images.shape[0], tf_img.shape[2], tf_img.shape[3])
        gt_img = torch.full(gt_img_shape, 255, dtype=torch.int64)
        data[0] = tf_img
        data[1] = gt_img
        
        val_idx = 0
        assets, _ = eval_minibatch(data, self.net, None, val_loss, False, self.args, val_idx)
        
        colorize_mask_fn = cfg.DATASET_INST.colorize_mask
        idx = 0  # only use first element of batch

        prediction = assets['predictions'][idx]
        prediction_pil = colorize_mask_fn(prediction)
        prediction_pil = prediction_pil.convert('RGB')
        pub_cv_img = np.array(prediction_pil)
        pub_cv_img = cv2.cvtColor(pub_cv_img, cv2.COLOR_RGB2BGR)
        ros_img_msg = self.bridge.cv2_to_imgmsg(pub_cv_img, encoding='bgr8')
        ros_img_msg.header = header_info

        self.pub.publish(ros_img_msg)
        prediction_pil.save('../debug/pred_img.png')
        rospy.loginfo('published image')


if __name__ == '__main__':
    semseg = SemanticSegmentation()
    try:
        rospy.init_node('labeling_node', anonymous=True)
        rospy.Subscriber('/semantickitti/camera_color_left/image_raw', ROS_Image, semseg.predict, queue_size=1, buff_size=2**32)
        rospy.spin()
    
    except rospy.ROSInterruptException:
        pass