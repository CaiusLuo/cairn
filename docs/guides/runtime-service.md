# Shared Runtime Service (experimental)

The opt-in Runtime owns task admission, a bounded queue, one worker, cancellation
and the ordered public event history. Entry adapters submit or read facts; they
never start their own execution for a Runtime task.

`runtime.task_service.TaskService` exposes submit, inspect, list_tasks, events,
follow, continue_task and cancel. `infra.runtime_catalog.RuntimeCatalogue` holds
host-private admission/worker facts under an exclusive process lease. A damaged
catalogue fails closed. After restart an observed running task becomes
recovery_required, never automatically replayed. Only safely paused tasks may
continue; queued tasks can be recovered without pretending they already ran.

The TaskBoard is a presentation projection, not an execution command. Durable
turn/checkpoint and effect authority remain with SessionStore/DurableSession and
the existing tool boundary; catalogue records cannot grant execution permission.

This is an experiment on feat/langgraph-spike. It must use a separate state
folder and endpoint from existing online WeChat/Web services.
