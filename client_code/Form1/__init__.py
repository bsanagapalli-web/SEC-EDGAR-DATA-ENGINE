from ._anvil_designer import Form1Template
from anvil import *
import anvil.server


class Form1(Form1Template):
  def __init__(self, **properties):
    super().__init__(**properties)
    self._all_history = []
    self.admin_results.visible = False
    self._configure_plots()
    self._analyze()

  def _configure_plots(self):
    for plot in (
      self.revenue_growth_plot,
      self.profitability_plot,
      self.free_cash_flow_plot,
      self.leverage_plot,
    ):
      plot.interactive = True
      plot.config = {"displayModeBar": False, "responsive": True}

  @handle("analyze_button", "click")
  def analyze_button_click(self, **event_args):
    self._analyze()

  @handle("ticker_input", "pressed_enter")
  def ticker_input_pressed_enter(self, **event_args):
    self._analyze()

  def _analyze(self):
    ticker = (self.ticker_input.text or "").strip().upper()
    if not ticker:
      self.status_label.text = "Enter a ticker to analyze"
      self.ticker_input.focus()
      return

    self.ticker_input.text = ticker
    self.analyze_button.enabled = False
    self.status_label.text = "Loading SEC filing data..."
    try:
      result = anvil.server.call("analyze_ticker", ticker, self.force_refresh.checked)
      if not result.get("ok"):
        self.status_label.text = result["message"]
        return
      self._apply_result(result)
    finally:
      self.analyze_button.enabled = True

  def _apply_result(self, result):
    metrics = result["metrics"]
    self.company_name.text = result["company_name"]
    self.company_ticker.text = result["ticker"]
    self.overview_as_of.text = "FY {}".format(result["latest_year"])
    self.visitor_count.text = str(result["visitor_count"])
    self.current_user_count.text = str(result["current_user_count"])
    self.status_label.text = "Analysis refreshed • SEC EDGAR"

    self.revenue_value.text = metrics["revenue"]
    self.revenue_delta.text = metrics["revenue_delta"]
    self.net_income_value.text = metrics["net_income"]
    self.net_income_delta.text = metrics["net_income_delta"]
    self.fcf_value.text = metrics["free_cash_flow"]
    self.fcf_delta.text = metrics["free_cash_flow_delta"]
    self.roe_value.text = metrics["roe"]
    self.roe_delta.text = metrics["roe_delta"]
    self.operating_margin_value.text = metrics["operating_margin"]
    self.net_margin_value.text = metrics["net_margin"]
    self.debt_equity_value.text = metrics["debt_equity"]
    self.current_ratio_value.text = metrics["current_ratio"]

    self._all_history = result["history"]
    self._filter_history()
    charts = result["charts"]
    self.revenue_growth_plot.figure = charts["revenue_growth"]
    self.profitability_plot.figure = charts["profitability"]
    self.free_cash_flow_plot.figure = charts["free_cash_flow"]
    self.leverage_plot.figure = charts["leverage"]

  @handle("usage_timer", "tick")
  def usage_timer_tick(self, **event_args):
    counts = anvil.server.call("get_public_user_counts")
    if counts.get("ok"):
      self.visitor_count.text = str(counts["all_time_users"])
      self.current_user_count.text = str(counts["current_users"])

  @handle("history_search", "change")
  def history_search_change(self, **event_args):
    self._filter_history()

  def _filter_history(self):
    query = (self.history_search.text or "").strip().lower()
    self._history = [
      row for row in self._all_history
      if not query or query in row["search_text"].lower()
    ]
    self.history_panel.items = self._history
    count = len(self._history)
    self.history_count.text = "%d fiscal year%s" % (count, "" if count == 1 else "s")

  @handle("download_button", "click")
  def download_button_click(self, **event_args):
    columns = ["Fiscal Year", "Revenue", "Net Income", "FCF", "Gross Margin", "Operating Margin", "ROE", "Debt / Equity"]
    keys = ["year", "revenue", "net_income", "fcf", "gross_margin", "operating_margin", "roe", "debt_equity"]
    lines = [",".join(columns)]
    lines.extend(
      ",".join(row[key] for key in keys)
      for row in self._all_history
    )
    ticker = (self.ticker_input.text or "company").strip().lower()
    media = BlobMedia("text/csv", "\n".join(lines).encode("utf-8"), name="%s_fundamentals.csv" % ticker)
    download(media)

  @handle("admin_button", "click")
  def admin_button_click(self, **event_args):
    admin_key = (self.admin_key_input.text or "").strip()
    if not admin_key:
      self.admin_status.text = "Enter the admin key"
      self.admin_key_input.focus()
      return

    self.admin_button.enabled = False
    self.admin_status.text = "Loading activity..."
    try:
      result = anvil.server.call("get_admin_usage", admin_key)
      if not result.get("ok"):
        self.admin_status.text = result["message"]
        self.admin_results.visible = False
        return
      events = result["events"]
      self.admin_panel.items = events
      self.admin_results.visible = True
      self.admin_status.text = "%d activity event%s" % (len(events), "" if len(events) == 1 else "s")
    finally:
      self.admin_button.enabled = True
