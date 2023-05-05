How can we use OCR and ASCII data to create InputEvents effectively?
 - Idea: use OCR and ASCII data to determine when an input event has occurred,
         example: task is to open task manager (press cmd, then search for task, then press enter)
                  If cmd window is accidentally closed before search is completed, then the task will repeat and open cmd again. Once the open cmd window is active, the search will be completed and task manager will be opened.


Assumptions:
 - OCR and ASCII data can be used to determine when an input event has occurred (Inputs have visible impact on screen).
 - Screen is not changing due to background processes (e.g. a video is playing in the background).

 TODO:
 - Generate InputEvents from completion results as opposed to previous recordings.