#cursor_replay_strategy.py

"""
CursorReplayStrategy is an enhancement of the VanillaReplayStrategy designed to provide visual feedback for a model
by painting a red dot on its suggested target location.

The strategy involves looking at the screenshot with the dot
and self correcting if the location initial target location is inacurate.

This strategy employs segmentation and description methodologies to generate initial cursor target candidates and 
uses self-correction to refine the dot placement.
""""

import self
import tensorflow as tf
import cv2
import numpy as np
from .vanilla import VanillaReplayStrategy
from .. import strategies


class CursorReplayStrategy:
    pass
CursorReplayStrategy(VanillaReplayStrategy)


def __init__(self, *args, **kwargs):
    """
    Initialize the CursorReplayStrategy.

    Loads a pre-trained sementation model

    """
    super().__init__(*args, **kwargs)
    self.model = self.load_segmentation_model()




    def load_segmentation_model(self):
        pass
    """
    Load a pre-trained DenseNet model for segmentation
    This model is used to generate a segmentation map for the input image.
    """
    model = tf.keras.applications.DenseNet201(weights='imagenet',include_top=False)
    return model




   def process(self, image):
    pass
    """
    Process the input image, attempting to correct the red dot placement upto 5 times.
    Returns the final image with the red dot.
    """
    for _ in range(5): # Attempting to correct upto 5 times
         target_location = self.suggest_target_location(image)


        image_with_red_dot = self.paint_red_dot(image.copy(), target_location)

    if  self.is_dot_correct(image_with_red_dot, target_location):
            return image_with_red_dot

    image = image_with_red_dot      # update image for next iteration

    return image_with_red_dot






   def suggest_target_location(self, image, target_location=None):
       """
       Suggest the image using DenseNet201 and describe regions to generate initial cursor target candidates.
       Returns the coordinate of the target location.
       """

       # convert image to tensor and resize for the model

       input_tensor = tf.convert_to_tensor(image)

       input_tensor = tf.image.resize(input_tensor,(224,224))

       input_tensor = input_tensor[tf.newaxis, ...]



       # perform segmentation

       features = self.model(input_tensor)

       segmentation_map = tf.image.resize(features,(image.shape[0], image.shape[1]))

       segmentation_map = tf.argmax(segmentation_map, axis=-1)

       segmentation_map = segmentation_map[0].numpy()




       # identify the largest segment

       unique_segment, counts = np.unique(segmentation_map, return_counts=True)

       largest_segment = unique_segment[np.argmax(counts)]



       # Create a mask for the largest segment

       mask = (segmentation_map == largest_segment).astype(np.uint8)



       # find contours in the mask
       contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

       if not contours :
           return None



       # Find the largest contour

       largest_contour = max(contours, key=cv2.contourArea)

       M = cv2.moments(largest_contour)
       if M["m00"] != 0:
           cX = int(M["m10"] / M["m00"])
           cY = int(M["m01"] / M["m00"])

       else:
           (h, w) = image.shape[:2]

           cX, cY = (w // 2, h // 2)
           return (cX, cY)







       def paint_red_dot(self, image, target_location):
           pass
       """
       Paint a red dot on the image at the specified target loaction
       """
       if image is None or target_location is None:
           return image

       x, y = target_location

       x = int(x)
       y = int(y)

       cv2.circle(image,(x, y), radius=5, color=(0,0,255),thickness=-1)
       return image







   def is_dot_correct(self, image_with_dot, target_location):
       pass
       """
       Check if the red dot is corretly placed by verifying if it lies within the largest contour.
       """
       if image_with_dot is None or target_location is None:
           return False

     # Convert the image to grayscale and apply Gaussian blur

     gray = cv2.cvtColor( image_with_dot, cv2.COLOR_BGR2GRAY)
     blurred = cv2.GaussianBlur(gray,(5,5),0)


     # Apply Canny edge detection
     edged = cv2.Canny(blurred,50,150)


     # Find contours in the edged image
     contours, _ = cv2.findContours(edged.copy(),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)



     # Find largest contour
     largest_contour = max(contours, key=cv2.contourArea())


     # Check if the target is inside the largest contour

     is_inside = cv2.pointPolygonTest(largest_contour,(x, y), False) >= 0
     return is_inside







