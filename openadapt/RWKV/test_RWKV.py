from openadapt.RWKV.RWKV import run_RWKV
import modal
import os
from openadapt import config


if __name__ == '__main__':
    Func = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
    parameters = config.RWKV_PARAMETERS

    a = Func.call(parameters=parameters)
    print(a)