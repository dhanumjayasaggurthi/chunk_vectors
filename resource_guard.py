"""Opt-in Linux admission backpressure; not a memory allocator or thermal sensor.

Host CPU/available memory and the service's own cgroup-v2 memory.current (when
available). Stop admitting new work under memory pressure; active work drains.
Use systemd CPUQuota/MemoryHigh/MemoryMax for the independent OS safety boundary.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

_GIB=1024**3
_FIELDS={"initial_workers","poll_seconds","recovery_seconds","max_pause_seconds",
         "cpu_high_percent","cpu_recover_percent","host_stop_available_gib",
         "host_resume_available_gib","group_stop_gib","group_resume_gib"}
_LIMITS={"initial_workers":(1,32),"poll_seconds":(1,60),"recovery_seconds":(1,3600),
         "max_pause_seconds":(1,86400),"cpu_high_percent":(1,100),
         "cpu_recover_percent":(1,100),"host_stop_available_gib":(1,1024),
         "host_resume_available_gib":(1,1024),"group_stop_gib":(1,1024),
         "group_resume_gib":(1,1024)}


def settings(value,workers):
    if not isinstance(value,dict) or set(value)!=_FIELDS:
        raise ValueError("Expected complete Linux resource guard profile")
    for k,(lo,hi) in _LIMITS.items():
        if type(value[k]) is not int or not lo<=value[k]<=hi:
            raise ValueError("Invalid resource guard "+k)
    if (value["initial_workers"]>workers
            or value["cpu_recover_percent"]>=value["cpu_high_percent"]
            or value["host_resume_available_gib"]<=value["host_stop_available_gib"]
            or value["group_resume_gib"]>=value["group_stop_gib"]
            or value["recovery_seconds"]<value["poll_seconds"]):
        raise ValueError("Invalid resource guard hysteresis or worker limits")
    return dict(value)


class LinuxMetrics:
    def __init__(self):
        if not sys.platform.startswith("linux"):
            raise ValueError("Resource guard requires Linux")
        self.previous=None
        self.group=None
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                rel=line[3:].lstrip("/")
                if rel and ".." not in Path(rel).parts:
                    path=Path("/sys/fs/cgroup")/rel/"memory.current"
                    if path.is_file():
                        self.group=path
                break

    def __call__(self):
        fields={}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name,sep,value=line.partition(":")
            if sep and name=="MemAvailable":
                fields[name]=int(value.split()[0])*1024
        available=fields["MemAvailable"]
        cpu=Path("/proc/stat").read_text().splitlines()[0].split()
        if cpu[0]!="cpu" or len(cpu)<9:
            raise RuntimeError("Linux CPU metrics unavailable")
        # guest and guest_nice are already included in user/nice.
        ticks=list(map(int,cpu[1:9]))
        total=sum(ticks)
        idle=ticks[3]+ticks[4]
        busy=None
        if self.previous:
            old_total,old_idle=self.previous
            delta=total-old_total
            if delta>0 and idle>=old_idle:
                busy=max(0.0,min(100.0,100*(1-(idle-old_idle)/delta)))
        self.previous=(total,idle)
        group=int(self.group.read_text().strip()) if self.group is not None else None
        return busy,available,group


class ResourceGuard:
    def __init__(self,config,workers,*,metrics=None,clock=time.monotonic):
        self.cfg=settings(config,workers)
        self.workers=workers
        self.metrics=metrics if metrics is not None else LinuxMetrics()
        self.clock=clock
        self.limit=self.cfg["initial_workers"]
        self.next_sample=None
        self.healthy_since=None
        self.paused=False
        self.last_reason="STARTING"
        self.samples=0
        self.reductions=0
        self.pauses=0
        self.increases=0

    def poll(self):
        now=self.clock()
        if self.next_sample is not None and now<self.next_sample:
            return self.limit
        self.next_sample=now+self.cfg["poll_seconds"]
        busy,available,group=self.metrics()
        self.samples+=1
        c=self.cfg
        stop=(available<=c["host_stop_available_gib"]*_GIB
              or group is not None and group>=c["group_stop_gib"]*_GIB)
        recover=(available>=c["host_resume_available_gib"]*_GIB
                 and (group is None or group<=c["group_resume_gib"]*_GIB)
                 and busy is not None and busy<=c["cpu_recover_percent"])
        if stop:
            if not self.paused:
                self.pauses+=1
            self.limit=0
            self.paused=True
            self.healthy_since=None
            self.last_reason="MEMORY_PRESSURE"
        elif self.paused:
            if not recover:
                self.healthy_since=None
            else:
                if self.healthy_since is None:
                    self.healthy_since=now
                if now-self.healthy_since>=c["recovery_seconds"]:
                    self.limit=1
                    self.paused=False
                    self.healthy_since=now
                    self.increases+=1
                    self.last_reason="RECOVERED"
        elif busy is not None and busy>=c["cpu_high_percent"]:
            old=self.limit
            self.limit=max(1,self.limit-1)
            self.reductions+=int(self.limit<old)
            self.healthy_since=None
            self.last_reason="CPU_PRESSURE"
        elif recover:
            if self.healthy_since is None:
                self.healthy_since=now
            if now-self.healthy_since>=c["recovery_seconds"]:
                old=self.limit
                self.limit=min(self.workers,self.limit+1)
                self.increases+=int(self.limit>old)
                self.healthy_since=now
                self.last_reason="HEALTHY"
        else:
            self.healthy_since=None
        return self.limit

    def report(self):
        return {"samples":self.samples,"cpu_reductions":self.reductions,
                "memory_pauses":self.pauses,"admission_increases":self.increases,
                "last_limit":self.limit,"last_reason":self.last_reason,
                "cgroup_metric_available":bool(getattr(self.metrics,"group",None))}
