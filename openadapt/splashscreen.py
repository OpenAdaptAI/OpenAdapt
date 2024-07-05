import sys , multiprocessing , threading , time

from PySide6 import QtCore, QtWidgets, QtGui 
from PySide6.QtWidgets import QApplication  ,  QSplashScreen , QMainWindow , QLabel
from PySide6.QtGui import QPixmap , QGuiApplication


def showUI():
    app = QApplication([])
    pixmap = QPixmap("splash.jpg")
    splash = QSplashScreen(pixmap)
    splash.show()
    app.processEvents()         

    centerPoint = QGuiApplication.screens()[0].geometry().center()
    window = QMainWindow() 
    window.setGeometry(centerPoint.x()-200, centerPoint.y()-150, 400, 300) 
    label = QLabel(window,alignment=QtCore.Qt.AlignCenter)
    label.resize(400, 400)
    label.move(0, -65)
    label.setPixmap(pixmap)
    label.show()
    window.setWindowFlag( QtCore.Qt.WindowStaysOnTopHint )
    window.setWindowIcon(QtGui.QIcon('logo.png'))
    window.setWindowTitle("OpenAdapt")
    # window.setWindowFlag(QtCore.Qt.FramelessWindowHint)   
    window.show()
    splash.finish(window) 
    sys.exit(app.exec())

showUI_process = multiprocessing.Process(target=showUI, args=( )) 

def show_splash_screen():
    global showUI_process 
    showUI_process.start() 

def terminate():
    global showUI_process
    time.sleep(4) # give more time to finish setup
    showUI_process.terminate()

def exit_splash_screen(): 
    ternimate_process = threading.Thread(target=terminate, args=( )) 
    ternimate_process.start()
    
