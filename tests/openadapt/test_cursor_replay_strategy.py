import unittest
import cv2
import numpy as np
import self

from openadapt.strategies.cursor_replay_strategy import CursorReplayStrategy

class TestCursorReplayStrategy(unittest.TestCase):

    def setUp(self, result_image=None, image=None):
        self.strategy = CursorReplayStrategy()
"""
Set up the CursorReplayStrategy instance for testing.
"""



        def test_paint_red_dot(self):
            """
            Test painting a red dot on the image at the specified target location.
            """

            image = np.zeroes((100,100,3), dtype="uint8")

            target_location = (50,50)

            result_image = self.strategy.paint_red_dot(image,target_location)

        # Check if the dot is painted by verifying the pixel value
        self.assertTrue((result_image[50,50] == [0,0,255]).all())








        def test_segment_and_describe(self):
         """
         Test the segmentation and description logic to ensure it correctly identifies the target location.
         """

            image = np.zeroes((100,100,3), dtype="uint8")

            cv2.rectangle(image,(30,30),(70,70),(255,255,255),-1)

        target_location = self.strategy.segment_and_describe(image)

        # the target location should be approximately the centre of the rectangle
        self.assertEqual(target_location,(50,50))





       def test_is_dot_correct(self):

           """
           Test the corrections check method to ensure it accurately verifies the red dot placement. 
           """"
           image = np.zeroes((100,100,3),dtype="uint8")

           cv2.rectangle(image, (30,30), (70,70), (255,255,255),-1)

           target_location = (50,50)

           image_with_dot = self.strategy.paint_red_dot(image,target_location)

           is_correct = self.strategy.is_dot_correct(image_with_dot, target_location)

           self.assertTrue(is_correct)






        def test_process_with_correction(self):
            """
            Test the entire process method to validate the correction process.
            """

            image = np.zeroes((100,100,3), dtype="uint8")

            cv2.rectangle(image,(30,30),(70,70),(255,255,255),-1)

            result_image = self.strategy.process(image)



        self.assertTrue((result_image[50,50] == [0,0,255]).all())

        if __name__ == '__main__':
            (unittest.main())
        
             

        if __name__ == '__main__':unittest.main()
