import rospy
import cv2
from cv_bridge import CvBridge
from sensor_msgs.msg import Image

bridge = CvBridge()

def store_img(msg):
    rospy.loginfo("storing image")
    img = bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
    cv2.imwrite('../debug/msg_img.png', img)

if __name__ == '__main__':
    rospy.init_node('debug')
    rospy.Subscriber('/pred_img', Image, store_img, queue_size=1, buff_size=2**32)
    rospy.spin()
