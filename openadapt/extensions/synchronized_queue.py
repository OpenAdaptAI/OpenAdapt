"""
    Module for customizing multiprocessing.Queue to avoid NotImplementedError in MacOS
"""


from multiprocessing.queues import Queue
import multiprocessing

# Credit: https://gist.github.com/FanchenBao/d8577599c46eab1238a81857bb7277c9

# The following implementation of custom SynchronizedQueue to avoid NotImplementedError
# when calling queue.qsize() in MacOS X comes almost entirely from this github
# discussion: https://github.com/keras-team/autokeras/issues/368
# Necessary modification is made to make the code compatible with Python3.


class SharedCounter(object):
    """ A synchronized shared counter.
    The locking done by multiprocessing.Value ensures that only a single
    process or thread may read or write the in-memory ctypes object. However,
    in order to do n += 1, Python performs a read followed by a write, so a
    second process may read the old value before the new one is written by the
    first process. The solution is to use a multiprocessing.Lock to guarantee
    the atomicity of the modifications to Value.
    This class comes almost entirely from Eli Bendersky's blog:
    http://eli.thegreenplace.net/2012/01/04/
    shared-counter-with-pythons-multiprocessing/
    """

    def __init__(self, n=0):
        self.count = multiprocessing.Value('i', n)

    def increment(self, n=1):
        """ Increment the counter by n (default = 1) """
        with self.count.get_lock():
            self.count.value += n

    @property
    def value(self):
        """ Return the value of the counter """
        return self.count.value


class SynchronizedQueue(Queue):
    """ A portable implementation of multiprocessing.Queue.
    Because of multithreading / multiprocessing semantics, Queue.qsize() may
    raise the NotImplementedError exception on Unix platforms like Mac OS X
    where sem_getvalue() is not implemented. This subclass addresses this
    problem by using a synchronized shared counter (initialized to zero) and
    increasing / decreasing its value every time the put() and get() methods
    are called, respectively. This not only prevents NotImplementedError from
    being raised, but also allows us to implement a reliable version of both
    qsize() and empty().
    Note the implementation of __getstate__ and __setstate__ which help to
    serialize SynchronizedQueue when it is passed between processes. If these functions
    are not defined, SynchronizedQueue cannot be serialized, which will lead to the error
    of "AttributeError: 'SynchronizedQueue' object has no attribute 'size'".
    See the answer provided here: https://stackoverflow.com/a/65513291/9723036
    
    For documentation of using __getstate__ and __setstate__ 
    to serialize objects, refer to here: 
    https://docs.python.org/3/library/pickle.html#pickling-class-instances
    """

    def __init__(self):
        super().__init__(ctx=multiprocessing.get_context())
        self.size = SharedCounter(0)

    def __getstate__(self):
        """Help to make SynchronizedQueue instance serializable.
        Note that we record the parent class state, which is the state of the
        actual queue, and the size of the queue, which is the state of SynchronizedQueue.
        self.size is a SharedCounter instance. It is itself serializable.
        """
        return {
            'parent_state': super().__getstate__(),
            'size': self.size,
        }

    def __setstate__(self, state):
        super().__setstate__(state['parent_state'])
        self.size = state['size']

    def put(self, *args, **kwargs):
        super().put(*args, **kwargs)
        self.size.increment(1)

    def get(self, *args, **kwargs):
        item = super().get(*args, **kwargs)
        self.size.increment(-1)
        return item

    def qsize(self):
        """ Reliable implementation of multiprocessing.Queue.qsize() """
        return self.size.value

    def empty(self):
        """ Reliable implementation of multiprocessing.Queue.empty() """
        return not self.qsize()
