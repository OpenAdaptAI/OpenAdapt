from openadapt.RWKV.RWKV import run_RWKV
import modal



if __name__ == '__main__':
    stub = modal.Stub("test-stub")
    with stub.run():
        run_RWKV.call()