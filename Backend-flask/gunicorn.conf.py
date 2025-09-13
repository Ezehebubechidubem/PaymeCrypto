bind = "0.0.0.0:8000"   # listen on all IPs, port 8000
workers = 4             # number of worker processes
threads = 2             # threads per worker
timeout = 120           # kill workers if they hang > 120s
preload_app = True      # load app before workers are forked