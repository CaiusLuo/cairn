"""Order startup and shutdown of the Server's process-owned work."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from cairn.conversation.service import ConversationService
from cairn.development.execution.runtime import ServiceExecution
from cairn.development.service import TaskService
from cairn.integrations.wechat.delivery import WechatDeliveryCoordinator
from cairn.review.service import DailyReviewService
from cairn.server.management import ServerControls
from cairn.shared.lifecycle import await_owned


@asynccontextmanager
async def running_runtime(
    execution: ServiceExecution,
    service: TaskService,
    conversations: ConversationService,
    controls: ServerControls,
    delivery: WechatDeliveryCoordinator,
    reviews: DailyReviewService | None = None,
) -> AsyncIterator[None]:
    try:
        _ = execution.store
        await service.start()
        await conversations.start()
        await controls.start()
        if reviews is not None:
            await reviews.start()
        await delivery.start()
        yield
    finally:
        primary = sys.exception()

        async def cleanup() -> None:
            try:
                await delivery.close()
            finally:
                try:
                    if reviews is not None:
                        await reviews.close()
                finally:
                    try:
                        await controls.close()
                    finally:
                        try:
                            await conversations.close()
                        finally:
                            try:
                                await service.close()
                            finally:
                                execution.close()

        owned = asyncio.create_task(cleanup())
        late = await await_owned(owned)
        owned.result()
        if late is not None and primary is None:
            raise late
