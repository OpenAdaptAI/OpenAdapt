from kivy.app import App
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.button import Button
from kivy.uix.image import Image
from kivy.core.window import Window
from openadapt.record import record
import time
import win32api
import win32gui
import win32con

class OpenAdapt(App) :
    def build(self):

        Window.size = (200, 200)
        Window.borderless = True
        Window.always_on_top = True
        Window.clearcolor = (255, 255, 255, 1)
        self.window = FloatLayout()
        self.button = Button(  background_normal="assets/logo.png",
                                        size_hint=(1, 1),
                                        pos_hint={"center_x" : 0.5, "center_y" : 0.5},
                                        text="hi")
        self.button.bind(on_press=self.callback)
        self.window.add_widget(self.button)
        return self.window
    def callback(self, instance):
        self.button.disabled = True
        self.button.background_normal = "assets/noun-recording.png"
        
        # self.window.remove_widget(instance)
        # self.image = Image(  source="assets/noun-recording.png",
        #                                 size_hint=(1, 1),
        #                                 pos_hint={"center_x" : 0.5, "center_y" : 0.5})
        #self.window.add_widget(self.image)
        #record("testing")
        #self.image.source = "assets/noun-video-file.png"

if __name__ == "__main__" :
    OpenAdapt().run()