// static/js/orders.js
$(document).ready(function() {
    // Search functionality for orders table
    $("#orderSearch").on("keyup", function() {
        var value = $(this).val().toLowerCase();
        $("#ordersTable tbody tr").filter(function() {
            $(this).toggle($(this).text().toLowerCase().indexOf(value) > -1)
        });
    });
    
    // Auto-refresh orders every 30 seconds
    let ordersRefreshInterval;
    
    function startOrdersRefresh() {
        ordersRefreshInterval = setInterval(function() {
            location.reload();
        }, 30000); // Refresh every 30 seconds
    }
    
    function stopOrdersRefresh() {
        clearInterval(ordersRefreshInterval);
    }
    
    // Start refresh when page is visible, stop when hidden
    $(document).on('visibilitychange', function() {
        if (document.visibilityState === 'visible') {
            startOrdersRefresh();
        } else {
            stopOrdersRefresh();
        }
    });
    
    // Initialize refresh if page is visible
    if (document.visibilityState === 'visible') {
        startOrdersRefresh();
    }
    
    // Refresh button
    $('#refreshOrders').click(function() {
        location.reload();
    });
});