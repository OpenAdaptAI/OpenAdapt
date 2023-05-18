from openadapt.signals import Signals


def test_add_url_signal():
    signals = Signals()
    signals.add_url_signal("https://www.google.com")
    signal_data = signals.return_signals()
    #Assuming google.com is not down
    assert signal_data is not None