import logging
import time
from pathlib import Path
from src.services import scheduling
from src.services.execution import claim, execute
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def cycle():
    # 顺序固定：先补齐挂起日报（晚到来源），再物化到期计划，最后重试中断发布并消费队列。
    scheduling.promote_waiting()
    scheduling.materialize_due()
    scheduling.retry_pending()
    lease = claim()
    if lease:
        execute(lease)


def main():
    while True:
        try:
            cycle()
            heartbeat.touch()
            wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
