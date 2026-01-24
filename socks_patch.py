# socks_patch.py
import socks

def apply_patch():
    """
    Patches socks.socksocket.setblocking to prevent infinite recursion 
    when using eventlet/gevent with PySocks.
    """
    if getattr(socks, "_patched_for_eventlet", False):
        return

    original_setblocking = socks.socksocket.setblocking

    def setblocking(self, flag):
        # Calculate what the timeout SHOULD be
        desired_timeout = None if flag else 0.0
        
        # If we are already at that timeout, do nothing.
        # This check breaks the recursion loop:
        # socks.setblocking -> socks.settimeout -> eventlet.settimeout -> socks.setblocking
        if self.gettimeout() == desired_timeout:
            return

        # Otherwise, call the original logic
        if flag:
            self.settimeout(None)
        else:
            self.settimeout(0.0)

    # Apply the patch
    socks.socksocket.setblocking = setblocking
    socks._patched_for_eventlet = True
    print("PySocks patched for Eventlet compatibility.")
