# -*- coding: utf-8 -*-
"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""


import logging
import time

import arrow

from alarm_backends import constants
from alarm_backends.core.cache import key
from alarm_backends.core.cache.strategy import StrategyCacheManager
from alarm_backends.core.control.strategy import Strategy
from alarm_backends.core.handlers import base
from alarm_backends.service.nodata.tasks import no_data_check
from core.errors.iam import PermissionDeniedError

logger = logging.getLogger("nodata")
# 每分钟运行一次，检测两个周期前的 access 数据，运行间隔需要保持一致，建议设置为每分钟的后半分钟时间段
EXECUTE_TIME_SECOND = 55


class NodataHandler(base.BaseHandler):
    def handle(self):
        while True:
            second = arrow.now().second
            if second == EXECUTE_TIME_SECOND:
                now_timestamp = arrow.utcnow().timestamp - constants.CONST_MINUTES
                strategy_ids = StrategyCacheManager.get_strategy_ids()
                published = []
                for strategy_id in strategy_ids:
                    strategy = Strategy(strategy_id)
                    for item in strategy.items:
                        if item.no_data_config.get("is_enabled"):
                            no_data_check(strategy_id, now_timestamp)
                            published.append(strategy_id)
                            break

                logger.info(
                    "[nodata] no_data_check published {}/{} strategy_ids: {}".format(
                        len(published), len(strategy_ids), published
                    )
                )
                time.sleep(1)
            else:
                time.sleep(0.9)


class NodataCeleryHandler(NodataHandler):
    def handle(self):
        # 进程总锁， 基于celery任务分发，分布式场景，master不需要多个
        service_key = key.SERVICE_LOCK_NODATA.get_key(strategy_id=0)
        client = key.SERVICE_LOCK_NODATA.client
        ret = client.set(service_key, time.time(), ex=key.SERVICE_LOCK_NODATA.ttl, nx=True)
        if not ret:
            time.sleep(0.5)
            return
        if arrow.now().second == EXECUTE_TIME_SECOND:
            logger.info("[nodata] get leader now")
            now_timestamp = arrow.utcnow().timestamp - constants.CONST_MINUTES
            strategy_ids = StrategyCacheManager.get_strategy_ids()
            published = []
            for strategy_id in strategy_ids:
                strategy = Strategy(strategy_id)
                try:
                    for item in strategy.items:
                        if item.no_data_config.get("is_enabled"):
                            no_data_check.delay(strategy_id, now_timestamp)
                            published.append(strategy_id)
                            break
                except PermissionDeniedError:
                    # 无效的计算平台数据表(计算平台表会针对数据源和业务进行鉴权)
                    continue

            logger.info(
                "[nodata] no_data_check.delay published {}/{} strategy_ids: {}".format(
                    len(published), len(strategy_ids), published
                )
            )
            if arrow.now().second == EXECUTE_TIME_SECOND:
                # 先缓1s，过了EXECUTE_TIME_SECOND时间窗口先。
                time.sleep(1)

        client.delete(service_key)
        # 等下一波（EXECUTE_TIME_SECOND）
        wait_for = (EXECUTE_TIME_SECOND + 60 - arrow.now().second) % 60
        logger.info("[nodata] wait {}s for next leader competition".format(wait_for))
        if wait_for > 1:
            time.sleep(wait_for)
