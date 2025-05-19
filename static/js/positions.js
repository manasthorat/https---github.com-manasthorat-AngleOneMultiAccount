// static/js/positions.js
$(document).ready(function() {
    // Search functionality for positions table
    $("#positionSearch").on("keyup", function() {
        var value = $(this).val().toLowerCase();
        $("#positionsTable tbody tr").filter(function() {
            $(this).toggle($(this).text().toLowerCase().indexOf(value) > -1)
        });
    });
    
    // Auto-refresh positions every 30 seconds
    let positionsRefreshInterval;
    
    function startPositionsRefresh() {
        positionsRefreshInterval = setInterval(function() {
            location.reload();
        }, 30000); // Refresh every 30 seconds
    }
    
    function stopPositionsRefresh() {
        clearInterval(positionsRefreshInterval);
    }
    
    // Start refresh when page is visible, stop when hidden
    $(document).on('visibilitychange', function() {
        if (document.visibilityState === 'visible') {
            startPositionsRefresh();
        } else {
            stopPositionsRefresh();
        }
    });
    
    // Initialize refresh if page is visible
    if (document.visibilityState === 'visible') {
        startPositionsRefresh();
    }
    
    // Refresh button
    $('#refreshPositions').click(function() {
        location.reload();
    });
    
    // Calculate total P&L
    let totalPnl = 0;
    $('.position-pnl').each(function() {
        totalPnl += parseFloat($(this).data('pnl') || 0);
    });
    
    $('#total-pnl-value').text('₹' + totalPnl.toLocaleString('en-IN', {
        maximumFractionDigits: 2,
        minimumFractionDigits: 2
    }));
    
    if (totalPnl >= 0) {
        $('#total-pnl-value').addClass('text-success');
    } else {
        $('#total-pnl-value').addClass('text-danger');
    }
});