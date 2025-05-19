





// static/js/common.js
// Common utility functions and behaviors for all pages

// Format currency values
function formatCurrency(value) {
    return parseFloat(value).toLocaleString('en-IN', {
        maximumFractionDigits: 2,
        minimumFractionDigits: 2
    });
}

// Debounce function to limit execution rate of handlers
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            timeout = null;
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Initialize tooltips everywhere
$(document).ready(function() {
    // Initialize all tooltips
    $('[data-bs-toggle="tooltip"]').tooltip();
    
    // Check for new notifications every minute
    setInterval(checkNotifications, 60000);
    
    function checkNotifications() {
        $.get('/api/notifications', function(data) {
            if (data.count > 0) {
                $('#notification-badge').text(data.count).show();
            } else {
                $('#notification-badge').hide();
            }
        });
    }
});