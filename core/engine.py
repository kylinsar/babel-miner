"""Native stdin/stdout worker loop. Runs until --seconds; a HIT does not end the session.
Plugins supply fetch/spawn/on_hit. Kernel never retries broadcast. Budget/max_txs stop new signatures."""
import json
import selectors
import time
from core.safety import SafetyError, RpcReadError, after_poll_error, log, parse_cuda_devices


def run_native(*, command, address, backend, devices, threads, seconds, poll, batch,
               fetch_job, show_job, spawn_worker, selftest, on_hit, job_target=lambda j: getattr(j, 'target', 0),
               job_label=lambda j: f'laid={getattr(j, "laid", "?")}'):
    bench = command == 'bench'
    if bench:
        address = address or '0x' + '11' * 20
    job = fetch_job()
    if not bench:
        show_job(job, address)
    device_ids = ['cpu'] * threads if backend == 'cpu' else parse_cuda_devices(devices)
    workers = []
    sel = selectors.DefaultSelector()
    try:
        for d in device_ids:
            w = spawn_worker(d)
            workers.append(w)
            selftest(w)
            sel.register(w.p.stdout, selectors.EVENT_READ, w)
        batch = batch or (4096 if backend == 'cpu' else 1 << 20)
        start = time.monotonic()
        deadline = start + seconds
        last_poll = start
        last_good = start
        last_log = start
        hashes = 0
        poll_wait = poll
        for w in workers:
            w.send(job, address, batch)
        while time.monotonic() < deadline:
            now = time.monotonic()
            if not bench and now - last_poll >= poll_wait:
                try:
                    fresh = fetch_job(deadline=deadline, poll=True)
                except RpcReadError as exc:
                    poll_wait = after_poll_error(str(exc), job, poll_wait, last_good, time.monotonic())
                    last_poll = time.monotonic()
                else:
                    if fresh.identity() != job.identity():
                        job = fresh
                        target = job_target(job)
                        eta = (1 << 256) / target if target else 0
                        log(f'新任务 {job_label(job)} E[hashes]={eta:.6g}' if target else f'新任务 {job_label(job)}')
                    last_poll = last_good = time.monotonic()
                    poll_wait = poll
            if time.monotonic() >= deadline:
                break
            for key, _ in sel.select(min(.2, max(0, deadline - time.monotonic()))):
                w = key.data
                old, nonce, count, found = w.read()
                hashes += count
                if time.monotonic() >= deadline:
                    break
                if found and old.identity() == job.identity():
                    fresh = fetch_job(deadline=deadline)
                    if fresh.identity() == old.identity():
                        on_hit(old, address, nonce, deadline)
                    job = fetch_job(deadline=deadline)
                w.send(job, address, batch)
            now = time.monotonic()
            if any(w.p.poll() is not None for w in workers):
                raise SafetyError('计算进程已退出')
            if any(now - w.sent > 60 for w in workers):
                raise SafetyError('计算批次超过60秒；请减小 --batch')
            if now - last_log >= 5:
                rate = hashes / (now - start)
                target = job_target(job)
                eta = (1 << 256) / target / rate if target and rate else 0
                log(f'{len(workers)} workers {rate/1e9:.6f} GH/s hashes={hashes}' + (f' eta~{eta/3600:.2f}h（统计平均）' if target else ''))
                last_log = now
        elapsed = time.monotonic() - start
        print(json.dumps({'type': 'bench' if bench else 'stopped', 'hashes': hashes, 'seconds': round(elapsed, 3), 'hps': int(hashes / elapsed)}), flush=True)
    finally:
        for w in workers:
            w.close()
        sel.close()
