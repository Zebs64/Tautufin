// Profil utilisateur : graphiques de genres et d'habitudes de visionnage.
const USER_ID = document.currentScript.dataset.userId;
// Barres exprimées en temps de visionnage : les données arrivent en secondes,
// l'axe et l'infobulle affichent des heures.
function userHoursBar(canvasId, data, color) {
  const secs = data.series[0] ? data.series[0].data : [];
  new Chart(document.getElementById(canvasId), {
    type: 'bar',
    data: {
      labels: data.categories,
      datasets: [{data: secs.map(s => s / 3600), backgroundColor: color}],
    },
    options: {responsive: true, maintainAspectRatio: false,
              plugins: {legend: {display: false},
                        tooltip: {callbacks: {label: c => fmtDuration(secs[c.dataIndex])}}},
              scales: {y: {beginAtZero: true,
                           ticks: {callback: v => v + ' h'}}}},
  });
}
const hasUserGraphData = data => Boolean((data.series || []).some(series =>
  (series.data || []).some(value => Number(value) > 0)));
ChartState.load('chart-user-genres',
  () => fetchJSON(`/api/graphs/by_genre?days=0&user_id=${encodeURIComponent(USER_ID)}`),
  hasUserGraphData,
  d => radarChart('chart-user-genres', d.categories, d.series[0].data));
ChartState.load('chart-user-dow',
  () => fetchJSON(`/api/graphs/by_day_of_week?days=0&metric=duration&user_id=${encodeURIComponent(USER_ID)}`),
  hasUserGraphData,
  d => userHoursBar('chart-user-dow', d, CHART_COLORS[0]));
ChartState.load('chart-user-hour',
  () => fetchJSON(`/api/graphs/by_hour_of_day?days=0&metric=duration&user_id=${encodeURIComponent(USER_ID)}`),
  hasUserGraphData,
  d => userHoursBar('chart-user-hour', d, CHART_COLORS[1]));
