import azure.functions as func

app = func.FunctionApp()


@app.timer_trigger(schedule="0 0 7 * * *", arg_name="timer", run_on_startup=False)
def drift_check(timer: func.TimerRequest) -> None:
    """Placeholder - fleshed out in Task 5."""
