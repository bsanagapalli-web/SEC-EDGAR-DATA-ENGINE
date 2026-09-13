from ._anvil_designer import HistoryRowTemplate
from anvil import *


class HistoryRow(HistoryRowTemplate):
  def __init__(self, **properties):
    super().__init__(**properties)
