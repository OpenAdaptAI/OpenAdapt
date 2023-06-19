from kivy.app import App
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.button import Button
from kivy.uix.image import Image
from kivy.core.window import Window
from openadapt.record import record

class OpenAdapt(App) :
    def build(self):
        Window.size = (100, 100)
        Window.clearcolor = (255,255,255)
        self.window = FloatLayout()
        self.button = Button(  background_normal="assets/logo.png",
                                        size_hint=(1, 1),
                                        pos_hint={"center_x" : 0.5, "center_y" : 0.5},
                                        text="hi")
        self.button.bind(on_press=self.callback)
        self.window.add_widget(self.button)
        return self.window
    def callback(self, instance):
        self.window.remove_widget(instance)
        self.image = Image(  source="assets/record.png",
                                        size_hint=(1, 1),
                                        pos_hint={"center_x" : 0.5, "center_y" : 0.5})
        self.window.add_widget(self.image)

if __name__ == "__main__" :
    OpenAdapt().run()