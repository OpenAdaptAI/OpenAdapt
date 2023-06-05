from openadapt.RWKV.RWKV import run_RWKV
import modal
import os



if __name__ == '__main__':
    Func = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
    a = Func.call()
    print(a)