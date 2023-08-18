Instructions to fine-tune recordings using OpenAI

1. Instantiate an OpenAIFineTuner class and pass in the model name as a parameter.

2. Call the ```prepare_data_for_tuning``` method on a recording using its id, which distills the recording into ```Action```, ```Window``` event pairs and writes the 
necessary ```prompt``` and ```completion``` lines to a JSONL file.

3. Call ```check_data_for_tuning``` to make last minute changes to the JSONL file if needed, and follow the instructions in the command line as needed.

4. Finally, call ```tune_model``` on the file path returned from ```prepare_data_for_tuning```.