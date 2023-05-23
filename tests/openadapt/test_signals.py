from openadapt.signals import Signals


def test_add_file_signal():
    signals = Signals()
    signals.add_signal("tests/openadapt/test_signal_data.txt")
    signal_data = signals.return_signals()
    assert signal_data == ["test_data_success"]


def test_add_url_signal():
    signals = Signals()
    signals.add_signal("https://platform.openai.com/")
    signal_data = signals.return_signals()
    #TODO: change to a more specific url to see if gathered data is correct and usable
    assert signal_data[0] != None


def test_add_function_signal():
    signals = Signals()
    signals.add_signal("sample_package.sample_module.sample_function")
    signal_data = signals.return_signals()
    assert signal_data == ["Sample function success"]


def test_non_existent_file():
    signals = Signals()
    signals.add_signal("tests/openadapt/test_signal_data_non_existent.txt")
    signal_data = signals.return_signals()
    assert signal_data[0] == None

def test_access_private_members():
    signals = Signals()
    try:
        signals.__add_file_signal("tests/openadapt/test_signal_data.txt")
    except AttributeError:
        assert True
