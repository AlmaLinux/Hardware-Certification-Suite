#!/usr/bin/env sh

RED='\033[0;31m'
NC="\033[0m"

ERRORS_COUNT=0

function error() {
  ERRORS_COUNT=$((ERRORS_COUNT + 1))
  printf "${RED}error: $1${NC}\n"
}

echo '========== pre-checks section start =========='

if [[ $(egrep -c '(vmx|svm)' /proc/cpuinfo) == '0' ]]; then
  error "CPU feature (vmx|svm) is not supported"
fi

VIRTUALIZATION=$(lscpu | egrep 'Virtualization:\s+(AMD-V|VT-x)' | awk '{print $2;}')
if [[ "$VIRTUALIZATION" != 'AMD-V' && "$VIRTUALIZATION" != 'VT-x' ]]; then
  error "Unknown virtualization type: '$VIRTUALIZATION'; AMD-V or VT-x required"
fi

echo '========== pre-checks section end =========='

exit $ERRORS_COUNT
