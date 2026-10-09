from cairn.terminal.commands.context import CommandContext

MODEL_USAGE = "Usage: /model | /model list | /model use <name>"


def _failure_note(context: CommandContext, model_id: str) -> str:
    executor = context.model_executor
    failure = executor.failure_for(model_id) if executor is not None else None
    if failure is None:
        return ""
    detail = f": {failure.detail}" if failure.detail else ""
    return f" — last failure: {failure.category}{detail}"


def handle_model(context: CommandContext, args: list[str]) -> None:
    manager = context.model_manager
    if manager is None:
        print("No model configuration is available in this session.")
        return

    if not args:
        model = manager.current_model()
        print(f"Current model: {model.name} ({' -> '.join(model.model_ids)})")
    elif args == ["list"]:
        for model in manager.list_models():
            marker = "*" if model == manager.current_model() else " "
            print(f"{marker} {model.name}")
            for position, model_id in enumerate(model.model_ids, start=1):
                print(f"    {position}. {model_id}{_failure_note(context, model_id)}")
    elif len(args) == 2 and args[0] == "use" and context.select_model is not None:
        try:
            model = context.select_model(args[1])
        except ValueError as exc:
            print(exc)
            return
        print(f"Current model: {model.name} ({' -> '.join(model.model_ids)})")
    else:
        print(MODEL_USAGE)
