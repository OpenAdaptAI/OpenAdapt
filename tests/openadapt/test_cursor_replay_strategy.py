import unittest
import cv2
import numpy as np
from openadapt.strategies.cursor_replay_strategy import CursorReplayStrategy

class TestCursorReplayStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = CursorReplayStrategy()

        def test_paint_red_dot(self):
            # test the paint red dot method

            image = np.zeroes((100,100,3), dtype="uint8")
            target_location = (50,50)
            result_image = self.strategy.paint_red_dot(image,target_location)

        # Check if the dot is painted by verifying the pixel value
        self.assertTrue((result_image[50,50] == [0,0,255]).all())

        def
        test_suggest_target_location(self):
            #Create an image with a white rectangle on a black background
            image = np.zeroes((100,100,3), dtype="uint8")
            cv2.rectangle(image,(30,30),(70,70),(255,255,255),-1)

        target_location = self.strategy.suggest_target_location(image)

        # the target location should be approximately the centre of the rectangle
        self.assertEqual(target_location,(50,50))

        def test_process(self):
            # create an image with a white rectangle on a black backgraound
            image = np.zeroes((100,100,3), dtype="uint8")
            cv2.rectangle(image,(30,30),(70,70),(255,255,255),-1)
            result_image = self.strategy.process(image)

        #check if the dot is painted approximately at the centre of the rectangle

        self.assertTrue((result_image[50,50] == [0,0,255]).all())

        if __name__ == '__main__':
            uinttest.main()
        
             

        if __name__ == '__main__':unittest.main()
