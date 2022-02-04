#!/bin/bash

TIMEOUT=${1-"4h"}
LOG_FILE=${2-"cpu_testing.log"}

function log() {
  echo "[$(date)] $1" >>$LOG_FILE
}

if [ -z $(which stress-ng 2>/dev/null) ]; then
  log "stress-ng not installed"
  exit 1
fi

log "Run CPU stress test... ${TIMEOUT}"

# TODO: Can't pass output to var
stress-ng --cpu 0 --cpu-method all -t $TIMEOUT --metrics --verbose &>>$LOG_FILE 2>&1

CODE=$?

STATUSES[0]="Success"
STATUSES[1]="Error; incorrect user options or a fatal resource issue in the stress-ng stressor harness (for example, out of memory)."
STATUSES[2]="One or more stressors failed."
STATUSES[3]="One or more stressors failed to initialise because of lack of resources, for example ENOMEM (no memory), ENOSPC (no space on file system) or a missing or unimplemented system call."
STATUSES[4]="One or more stressors were not implemented on a specific architecture or operating system."
STATUSES[5]="A stressor has been killed by an unexpected signal."
STATUSES[6]="A stressor exited by exit(2) which was not expected and timing metrics could not be gathered."
STATUSES[7]="The bogo ops metrics maybe untrustworthy. This is most likely to occur when a stress test is terminated during the update of a bogo-ops counter such as when it has been OOM killed. A less likely reason is that the counter ready indicator has been corrupted."

log "${STATUSES[CODE]}"

if (($CODE != 0)); then
  log "Test FAILED!"
fi

cat $LOG_FILE
rm -f $LOG_FILE

exit $CODE
