import datetime
import os

LIMIT = 10

#folders = ["Documents", "Desktop", "Downloads"]
# ignore_patterns = [".DS_Store", ".git", ".vscode", "__pycache__", "node_modules"]

files = []

def atime_search(folders, ignore_patterns):
    for folder in folders:
        #for root, dirs, files_in_folder in os.walk(os.path.expanduser(f"~/{folder}")):
        for root, dirs, files_in_folder in os.walk(os.path.expanduser(f"C:/Users/{folder}")):
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and d not in ignore_patterns
            ]
            for file in files_in_folder:
                if file in ignore_patterns:
                    continue
                path = os.path.join(root, file)
                if os.path.islink(path):
                    continue
                try:
                    accessed_time = datetime.datetime.fromtimestamp(os.path.getatime(path))
                    files.append((path, accessed_time))
                except FileNotFoundError:
                    pass

    files.sort(key=lambda x: x[1])

    for path, accessed_time in files[-LIMIT:]:
        print(f"{accessed_time}: {path}")
    print(len(files))

if __name__ == "__main__":
    folders1 = ["avide"]
    ignore_patterns1 = [".DS_Store", ".git", ".vscode", "__pycache__", "node_modules", ".pak"]
    folders2 = ["avide"]
    ignore_patterns2 = [".DS_Store", ".git", ".vscode", "__pycache__", "node_modules", ".pak", "AppData"]
    folders3 = ["Documents", "Desktop", "Downloads"]
    ignore_patterns3 = [".DS_Store", ".git", ".vscode", "__pycache__", "node_modules", ".pak"]


    time1 = datetime.datetime.now()
    atime_search(folders1, ignore_patterns1)
    time2 = datetime.datetime.now()
    atime_search(folders2, ignore_patterns2)
    time3 = datetime.datetime.now()
    atime_search(folders3, ignore_patterns3)
    time4 = datetime.datetime.now()

    print(f"""
    Time 1: {time2 - time1}, User directory
    Time 2: {time3 - time2}, User directory without AppData
    Time 3: {time4 - time3}, Documents, Desktop, Downloads directories          
""")


