"""Tests for metrics components."""


from openadapt_viewer.components import metrics_card, metrics_grid
from openadapt_viewer.components.metrics import domain_stats_grid


class TestMetricsCard:
    """Tests for metrics_card component."""

    def test_basic_card(self):
        """Test basic metrics card."""
        html = metrics_card(label="Total", value=100)
        assert "oa-metrics-card" in html
        assert "Total" in html
        assert "100" in html

    def test_card_with_float_value(self):
        """Test card with float value."""
        html = metrics_card(label="Rate", value=75.5)
        assert "75.5" in html

    def test_card_with_string_value(self):
        """Test card with string value."""
        html = metrics_card(label="Status", value="Active")
        assert "Active" in html

    def test_card_with_color(self):
        """Test card with color variant."""
        html = metrics_card(label="Passed", value=50, color="success")
        assert "oa-metrics-success" in html

    def test_card_with_change(self):
        """Test card with change indicator."""
        html = metrics_card(label="Growth", value=100, change=5.2)
        assert "+5.2%" in html
        assert "positive" in html

    def test_card_with_negative_change(self):
        """Test card with negative change."""
        html = metrics_card(label="Decline", value=80, change=-3.1)
        assert "-3.1%" in html
        assert "negative" in html

    def test_card_with_class_name(self):
        """Test card with additional CSS class."""
        html = metrics_card(label="Test", value=10, class_name="custom-card")
        assert "custom-card" in html


class TestMetricsGrid:
    """Tests for metrics_grid component."""

    def test_basic_grid(self):
        """Test basic metrics grid."""
        cards = [
            {"label": "A", "value": 1},
            {"label": "B", "value": 2},
        ]
        html = metrics_grid(cards)
        assert "oa-metrics-grid" in html
        # Count the actual card divs (class="oa-metrics-card")
        assert html.count('class="oa-metrics-card"') == 2

    def test_grid_with_columns(self):
        """Test grid with custom columns."""
        cards = [{"label": "A", "value": 1}]
        html = metrics_grid(cards, columns=3)
        assert "repeat(3, 1fr)" in html

    def test_grid_with_colors(self):
        """Test grid with colored cards."""
        cards = [
            {"label": "Pass", "value": 10, "color": "success"},
            {"label": "Fail", "value": 5, "color": "error"},
        ]
        html = metrics_grid(cards)
        assert "oa-metrics-success" in html
        assert "oa-metrics-error" in html


class TestDomainStatsGrid:
    """Tests for domain_stats_grid component."""

    def test_domain_stats(self):
        """Test domain statistics grid."""
        stats = {
            "office": {"passed": 5, "failed": 2, "total": 7},
            "browser": {"passed": 3, "failed": 1, "total": 4},
        }
        html = domain_stats_grid(stats)
        assert "oa-domain-stats-grid" in html
        assert "office" in html
        assert "browser" in html
        assert "5" in html
        assert "7" in html

    def test_domain_stats_percentage(self):
        """Test that percentage is calculated."""
        stats = {
            "test": {"passed": 1, "failed": 1, "total": 2},
        }
        html = domain_stats_grid(stats)
        assert "50%" in html

    def test_empty_domain_stats(self):
        """Test empty domain stats."""
        html = domain_stats_grid({})
        assert "oa-domain-stats-grid" in html
