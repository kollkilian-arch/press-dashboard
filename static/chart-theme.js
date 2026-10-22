(function () {
  'use strict';
  if (typeof Chart === 'undefined') return;

  Chart.register({
    id: 'appTheme',
    beforeUpdate: function (chart) {
      const styles = getComputedStyle(document.documentElement);
      const text = styles.getPropertyValue('--brand-muted').trim();
      const border = styles.getPropertyValue('--brand-border').trim();
      chart.options.color = text;
      if (chart.options.plugins.legend) chart.options.plugins.legend.labels.color = text;
      Object.values(chart.scales).forEach(function (scale) {
        scale.options.ticks.color = text;
        scale.options.grid.color = border;
        scale.options.border.color = border;
      });
    }
  });

  window.addEventListener('themechange', function () {
    Object.values(Chart.instances).forEach(function (chart) { chart.update('none'); });
  });
})();
