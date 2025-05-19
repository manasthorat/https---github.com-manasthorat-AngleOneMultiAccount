// static/js/dashboard.js
$(document).ready(function() {
    // Auto-refresh dashboard data every 60 seconds
    let dashboardRefreshInterval;
    
    function startDashboardRefresh() {
        dashboardRefreshInterval = setInterval(function() {
            $.get('/api/accounts/status', function(data) {
                const activeCount = data.filter(account => account.status === 'Active').length;
                $('#active-accounts-count').text(activeCount + '/' + data.length);
            });
            
            // Refresh account summaries
            updateAccountSummaries();
        }, 60000); // Refresh every 60 seconds
    }
    
    function stopDashboardRefresh() {
        clearInterval(dashboardRefreshInterval);
    }
    
    // Start refresh when page is visible, stop when hidden
    $(document).on('visibilitychange', function() {
        if (document.visibilityState === 'visible') {
            startDashboardRefresh();
        } else {
            stopDashboardRefresh();
        }
    });
    
    // Initialize refresh if page is visible
    if (document.visibilityState === 'visible') {
        startDashboardRefresh();
    }
    
    // Function to update account summaries
    function updateAccountSummaries() {
        $.get('/api/accounts/summary', function(data) {
            // Update summary cards
            $('#total-balance').text('₹' + formatCurrency(data.total_balance));
            $('#total-positions').text(data.total_positions);
            $('#total-pnl').text('₹' + formatCurrency(data.total_pnl));
            
            // Update the class based on P&L (positive or negative)
            if (data.total_pnl >= 0) {
                $('#pnl-card').removeClass('bg-danger').addClass('bg-success');
            } else {
                $('#pnl-card').removeClass('bg-success').addClass('bg-danger');
            }
        });
    }
    
    // Helper function to format currency
    function formatCurrency(value) {
        return parseFloat(value).toLocaleString('en-IN', {
            maximumFractionDigits: 2,
            minimumFractionDigits: 2
        });
    }
});
