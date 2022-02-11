#!/bin/bash

FULL_LOG=/root/full-ltp.log

# Run LTP tests
echo "LTP tests started"
date

#modprobe -r btrfs
#find /lib/modules/`uname -r` -type f -iname '*btrfs*' -delete
#rm -rf /opt/ltp/testcases/bin/{fork12,futex_wait03,msgstress04}

if [ "$1" = "all" ]; then
    /opt/ltp/runltp >> $FULL_LOG
else
    /opt/ltp/runltp -s $1 >> $FULL_LOG
fi

echo " "
echo "LTP tests ended"
date

cat `find /opt/ltp/results -name '*log'`

echo "Full log with ltp tests results can be found on SUT here: $FULL_LOG and /opt/ltp/results"
echo "success"

