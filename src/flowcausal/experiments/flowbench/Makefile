CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror

.PHONY: all clean smoke

all: flowbench

flowbench: flowbench.c
	$(CC) $(CFLAGS) $< -o $@

smoke: flowbench
	python3 run_flowbench.py --sessions 2 --trials 1 --stages 2 --cohort 4 --work-ms 5 --tail-work-ms 12 --out-dir /tmp/flowcausal-flowbench-smoke

clean:
	rm -f flowbench
