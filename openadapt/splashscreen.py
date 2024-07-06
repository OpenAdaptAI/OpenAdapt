import sys , multiprocessing , threading , time

from PySide6 import QtCore, QtWidgets, QtGui 
from PySide6.QtWidgets import QApplication  ,  QSplashScreen , QMainWindow , QLabel
from PySide6.QtGui import QPixmap , QGuiApplication
from loguru import logger


label_2 = False
def update_label():
    try:
        global label_2 
        currText = label_2.text()
        if(currText == "Loading." ) :
            currText = "Loading.." 
        elif(currText == "Loading.." ) :
            currText = "Loading..." 
        elif(currText == "Loading..." ) :
            currText = "Loading." 
        label_2.setText(currText)
    except Exception as e : 
        logger.error(" Error in updating splash screen : "+ str(e) )

def showUI():
    try :
        app = QApplication([])
        pixmap = QPixmap("splash.jpg")
        pixmap = QtGui.QPixmap(pixmap.scaledToWidth(820))
        app.processEvents()         

        global label_2
        centerPoint = QGuiApplication.screens()[0].geometry().center()
        window = QMainWindow() 
        window.setGeometry(centerPoint.x()-400, centerPoint.y()-300, 800, 600) 
        label = QLabel(window,alignment=QtCore.Qt.AlignCenter)
        label.resize(800, 600)
        label.move(0, 0)
        label.setPixmap(pixmap)
        label.show()
        timer = QtCore.QTimer(window)
        timer.timeout.connect( update_label)
        timer.start(1500)
        myFont=QtGui.QFont('Times', 13)
        myFont.setBold(True)  
        label_2 = QLabel('Loading.', window) 
        label_2.setFont(myFont)
        label_2.setStyleSheet('color: white')
        label_2.move(340, 560)
        label_2.show()
        window.setWindowFlag( QtCore.Qt.WindowStaysOnTopHint )
        window.setWindowIcon(QtGui.QIcon('logo.png'))
        # window.setWindowTitle("OpenAdapt")
        window.setWindowFlag(QtCore.Qt.FramelessWindowHint)   
        window.show()
        sys.exit(app.exec())
    except Exception as e : 
        logger.error(" Error in splash screen : "+ str(e) )

showUI_process = multiprocessing.Process(target=showUI, args=( )) 

def show_splash_screen():
    try:
        global showUI_process 
        showUI_process.start() 
    except Exception as e : 
        logger.error(" Error in starting splash screen process : "+ str(e) )

def terminate():
    try:
        global showUI_process
        time.sleep(5) # give more time to finish setup
        showUI_process.terminate()
    except Exception as e : 
        logger.error(" Error in terminating splash screen process : "+ str(e) )

def exit_splash_screen(): 
    try:
        ternimate_process = threading.Thread(target=terminate, args=( )) 
        ternimate_process.start()
    except Exception as e : 
        logger.error(" Error in stoping splash screen : "+ str(e) )
    
