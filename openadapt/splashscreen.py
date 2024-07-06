import sys , multiprocessing , threading , time

from PySide6 import QtCore, QtWidgets, QtGui 
from PySide6.QtWidgets import QApplication  ,  QSplashScreen , QMainWindow , QLabel
from PySide6.QtGui import QPixmap , QGuiApplication
from loguru import logger

queue = multiprocessing.Queue()
label_2 = False
label_3 = False

def update_progress(num):
    try:
        global queue
        queue.put(num)
    except Exception as e : 
        logger.error(" Error in updating progress : "+ str(e) )


def update_label():
    try:
        global label_2 , label_3, queue
        currText = label_2.text()
        if(currText == "Loading." ) :
            currText = "Loading.." 
        elif(currText == "Loading.." ) :
            currText = "Loading..." 
        elif(currText == "Loading..." ) :
            currText = "Loading." 
        label_2.setText(currText)
        count = queue.qsize()
        if count > 0 : 
            event = queue.get()
            label_3.setText(event)
    except Exception as e : 
        logger.error(" Error in updating splash screen : "+ str(e) )

def showUI(process_que):
    try :
        app = QApplication([])
        pixmap = QPixmap("splash.jpg")
        pixmap = QtGui.QPixmap(pixmap.scaledToWidth(820))
        app.processEvents()         

        global label_2 , label_3, queue 
        queue = process_que 
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
        timer.start(400)
        myFont=QtGui.QFont('Times', 13)
        myFont.setBold(True)  
        label_2 = QLabel('Loading.', window) 
        label_2.setFont(myFont)
        label_2.setStyleSheet('color: white')
        label_2.move(320, 560)
        label_2.show()
        label_3 = QLabel('5%', window) 
        label_3.setFont(myFont)
        label_3.setStyleSheet('color: white')
        label_3.move(430, 560)
        label_3.show()
        window.setWindowFlag( QtCore.Qt.WindowStaysOnTopHint )
        window.setWindowIcon(QtGui.QIcon('logo.png'))
        # window.setWindowTitle("OpenAdapt")
        window.setWindowFlag(QtCore.Qt.FramelessWindowHint)   
        window.show()
        sys.exit(app.exec())
    except Exception as e : 
        logger.error(" Error in splash screen : "+ str(e) )

showUI_process = multiprocessing.Process(target=showUI, args=(queue,)) 

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
    
