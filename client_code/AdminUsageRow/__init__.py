from ._anvil_designer import AdminUsageRowTemplate
from anvil import *


class AdminUsageRow(AdminUsageRowTemplate):
  def __init__(self, **properties):
    super().__init__(**properties)
