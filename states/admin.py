from aiogram.fsm.state import StatesGroup, State


class AdminState(StatesGroup):
    choosing_segment = State()
    entering_broadcast_content = State()
    confirming_broadcast = State()
    entering_repeat_days = State()
    editing_step_text = State()
    editing_step_photo = State()
    adding_admin = State()

    # Режим активного чата администратора с клиентом
    chatting_with_client = State()