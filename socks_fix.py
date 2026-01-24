import socks

# This patch fixes the infinite recursion error between eventlet and PySocks
# caused by cross-calling settimeout/setblocking.
def setblocking(self, flag):
    # Determine the corresponding timeout value: None for blocking, 0.0 for non-blocking
    timeout = None if flag else 0.0
    
    # If the timeout is already set to the desired value, do nothing.
    # This check breaks the recursion loop:
    # settimeout(None) -> sets _timeout=None -> super().settimeout(None) 
    # -> eventlet calls setblocking(True) -> we check here -> returns.
    if self.gettimeout() == timeout:
        return
        
    # Otherwise, set the timeout which effectively sets blocking mode
    self.settimeout(timeout)

# Apply the monkey patch to the PySocks class
socks.socksocket.setblocking = setblocking
