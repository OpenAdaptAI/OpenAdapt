from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

observed_files = []
whitelist = [".txt",".py","xlsx",".xls"]

class MyHandler(FileSystemEventHandler):
    def on_modified(self, event):
        for ext in whitelist:
            if event.src_path.endswith(ext):
                print(f"File {event.src_path} has been modified.")
                observed_files.append(event.src_path)

observer = Observer()
observer.schedule(MyHandler(), path='C://', recursive=True)
observer.start()

try:
    while True:
        # keep running
        pass
except KeyboardInterrupt:
    observer.stop()

observer.join()

print(observed_files)

 