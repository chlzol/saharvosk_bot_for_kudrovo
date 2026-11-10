from aiogram.fsm.state import StatesGroup, State


class BookingState(StatesGroup):
    choosing_branch = State()
    choosing_category = State()
    choosing_subcategory = State()
    choosing_sub2 = State()
    choosing_service = State()
    managing_cart = State()
    choosing_master = State()
    choosing_date = State()
    choosing_time = State()
    entering_phone = State()
    entering_name = State()
    confirming = State()


class SupportState(StatesGroup):
    choosing_branch = State()
    chatting = State()


class ReviewState(StatesGroup):
    entering_feedback = State()