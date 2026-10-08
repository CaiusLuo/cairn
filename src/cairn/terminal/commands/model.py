from cairn.terminal.commands.context import CommandContext

MODEL_USAGE = "Usage: /model | /model list | /model use <name>"


def handle_model(context: CommandContext, args: list[str]) -> None:
    manager = context.model_manager
    if manager is None:
        print("No model configuration is available in this session.")
        return

    if not args:
        model = manager.current_model()
        print(f"Current model: {model.name} ({model.model_id})")
    elif args == ["list"]:
        for model in manager.list_models():
            marker = "*" if model == manager.current_model() else " "
            print(f"{marker} {model.name} ({model.model_id})")
    elif len(args) == 2 and args[0] == "use" and context.select_model is not None:
        try:
            model = context.select_model(args[1])
        except ValueError as exc:
            print(exc)
            return
        print(f"Current model: {model.name} ({model.model_id})")
    else:
        print(MODEL_USAGE)
