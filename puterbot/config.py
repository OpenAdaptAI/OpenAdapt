import json
import pathlib

ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DB_FNAME = "puterbot.db"

DB_FPATH = ROOT_DIRPATH / DB_FNAME
DB_URL = f"sqlite:///{DB_FPATH}"
DB_ECHO = False

# Load the OpenAI key from keys.json
# Note that the file should be formatted as so: '{ "openai": "[key]" }'
KEYS_FNAME = "keys.json"
KEYS_FPATH = ROOT_DIRPATH / KEYS_FNAME
with open(KEYS_FPATH) as key_file:
	key_text = key_file.read()
	key_data = json.loads(key_text)

OPENAI_APIKEY = ""
if "openai" in key_data:
    OPENAI_APIKEY = key_data['openai']

OPENAI_SYSTEM_MESSAGE = '''The user will prompt you with a list of commands in angular brackets separated by spaces.
Each command has a keyword followed by parameters as so: <keyword parameter1 parameter2 parameter3>.
There can be one or more parameters.

Note that the first or the last command may be truncated. Discard any truncated commands.
Eliminate commands that have same keyword as the previous command.

An example command sequence could be:
201 304> <move 202 304> <click 1036 696 left True> <click 1036 696 left> <move 203 304> <move 205 305> <press Key.ctrl_a> <press Key.ctrl_a> <press Key.ctrl_a> <press

For the example above, the correct answer would be:
<move 202 304> <click 1036 696 left True> <move 203 304> <press Key.ctrl_a>
'''
