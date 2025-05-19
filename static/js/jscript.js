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

// static/js/symbol_search.js
$(document).ready(function() {
    $('#symbolSearchForm').submit(function(e) {
        e.preventDefault();
        
        const exchange = $('#exchange').val();
        const symbol_query = $('#symbol_query').val();
        
        if (symbol_query.length < 3) {
            alert("Please enter at least 3 characters to search");
            return;
        }
        
        $('#searchBtn').html('<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Searching...');
        $('#searchBtn').prop('disabled', true);
        
        $.ajax({
            url: '/search_symbols',
            type: 'POST',
            data: {
                exchange: exchange,
                symbol_query: symbol_query
            },
            success: function(response) {
                $('#searchBtn').html('Search');
                $('#searchBtn').prop('disabled', false);
                
                if (response.status === 'success') {
                    displayResults(response.symbols);
                } else {
                    $('#searchResults').html(`
                        <div class="alert alert-danger">
                            ${response.message}
                        </div>
                    `);
                }
            },
            error: function() {
                $('#searchBtn').html('Search');
                $('#searchBtn').prop('disabled', false);
                
                $('#searchResults').html(`
                    <div class="alert alert-danger">
                        An error occurred while searching. Please try again.
                    </div>
                `);
            }
        });
    });
    
    function displayResults(symbols) {
        if (symbols.length === 0) {
            $('#searchResults').html(`
                <div class="alert alert-warning">
                    No symbols found matching your search criteria.
                </div>
            `);
            return;
        }
        
        let tableHtml = `
            <div class="table-responsive">
                <table class="table table-striped table-hover" id="symbolsTable">
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Token</th>
                            <th>Exchange</th>
                            <th>Expiry</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
        `;
        
        symbols.forEach(symbol => {
            tableHtml += `
                <tr>
                    <td>${symbol.tradingsymbol || symbol.name}</td>
                    <td>${symbol.token}</td>
                    <td>${symbol.exch_seg}</td>
                    <td>${symbol.expiry || 'N/A'}</td>
                    <td>
                        <button class="btn btn-sm btn-primary copy-btn" 
                            data-symbol="${symbol.tradingsymbol || symbol.name}" 
                            data-token="${symbol.token}" 
                            data-exchange="${symbol.exch_seg}">
                            Copy to Webhook
                        </button>
                    </td>
                </tr>
            `;
        });
        
        tableHtml += `
                    </tbody>
                </table>
            </div>
        `;
        
        $('#searchResults').html(tableHtml);
        
        // Search functionality for results table
        $("#resultSearch").on("keyup", function() {
            var value = $(this).val().toLowerCase();
            $("#symbolsTable tbody tr").filter(function() {
                $(this).toggle($(this).text().toLowerCase().indexOf(value) > -1)
            });
        });
        
        // Copy button functionality
        $('.copy-btn').click(function() {
            const symbol = $(this).data('symbol');
            const token = $(this).data('token');
            const exchange = $(this).data('exchange');
            
            // Create a simple webhook template
            const webhook = {
                "webhook_key": "your_webhook_secret_key",
                "action": "BUY",
                "symbol": symbol,
                "symbol_token": token,
                "exchange": exchange,
                "product_type": "INTRADAY",
                "order_type": "MARKET",
                "quantity": 1
            };
            
            // Store in localStorage and redirect to webhook generator
            localStorage.setItem('webhook_template', JSON.stringify(webhook));
            window.location.href = '/webhook_generator';
        });
    }
});

// static/js/webhook_generator.js
$(document).ready(function() {
    // Set the current server address in the webhook URL field
    const currentHost = window.location.hostname;
    const currentPort = window.location.port;
    let webhookUrl = 'http://' + currentHost;
    if (currentPort) {
        webhookUrl += ':' + currentPort;
    }
    webhookUrl += '/webhook';
    $('#webhookUrl').val(webhookUrl);
    
    // Show/hide trigger price section based on order type
    $('#order_type').change(function() {
        const orderType = $(this).val();
        if (orderType.includes('STOPLOSS')) {
            $('#triggerPriceSection').show();
        } else {
            $('#triggerPriceSection').hide();
        }
    });
    
    // Generate webhook JSON when form is submitted
    $('#webhookForm').submit(function(e) {
        e.preventDefault();
        
        const webhookData = {
            "webhook_key": $('#webhook_key').val() || "your_webhook_secret_key",
            "action": $('#action').val(),
            "symbol": $('#symbol').val(),
            "symbol_token": $('#symbol_token').val(),
            "exchange": $('#exchange').val(),
            "product_type": $('#product_type').val(),
            "order_type": $('#order_type').val(),
            "quantity": parseInt($('#quantity').val())
        };
        
        // Add optional fields if they have values
        const price = $('#price').val();
        if (price) {
            webhookData.price = price;
        }
        
        const takeProfit = $('#take_profit').val();
        if (takeProfit) {
            webhookData.takeProfit = takeProfit;
        }
        
        const stopLoss = $('#stop_loss').val();
        if (stopLoss) {
            webhookData.stopLoss = stopLoss;
        }
        
        const triggerPrice = $('#trigger_price').val();
        if (triggerPrice) {
            webhookData.trigger_price = triggerPrice;
        }
        
        // Display the generated JSON
        $('#webhookJson').text(JSON.stringify(webhookData, null, 2));
    });
    
    // Copy webhook JSON to clipboard
    $('#copyJson').click(function() {
        const json = $('#webhookJson').text();
        navigator.clipboard.writeText(json).then(function() {
            alert('Webhook JSON copied to clipboard!');
        });
    });
    
    // Copy webhook URL to clipboard
    $('#copyUrlBtn').click(function() {
        const url = $('#webhookUrl').val();
        navigator.clipboard.writeText(url).then(function() {
            alert('Webhook URL copied to clipboard!');
        });
    });
    
    // Save template
    $('#saveTemplateBtn').click(function() {
        const templateName = prompt('Enter a name for this template:');
        if (!templateName) return;
        
        // Get current form values
        const template = {
            name: templateName,
            action: $('#action').val(),
            symbol: $('#symbol').val(),
            symbol_token: $('#symbol_token').val(),
            exchange: $('#exchange').val(),
            product_type: $('#product_type').val(),
            order_type: $('#order_type').val(),
            quantity: $('#quantity').val(),
            price: $('#price').val(),
            take_profit: $('#take_profit').val(),
            stop_loss: $('#stop_loss').val(),
            trigger_price: $('#trigger_price').val()
        };
        
        // Get existing templates from localStorage
        let templates = JSON.parse(localStorage.getItem('webhook_templates') || '[]');
        
        // Add new template
        templates.push(template);
        
        // Save to localStorage
        localStorage.setItem('webhook_templates', JSON.stringify(templates));
        
        // Refresh templates display
        loadSavedTemplates();
        
        alert('Template saved successfully!');
    });
    
    // Load saved templates from localStorage
    function loadSavedTemplates() {
        const templates = JSON.parse(localStorage.getItem('webhook_templates') || '[]');
        
        if (templates.length === 0) {
            $('#savedTemplates').html('<div class="alert alert-info">No saved templates yet.</div>');
            return;
        }
        
        let html = '<div class="list-group">';
        templates.forEach((template, index) => {
            html += `
                <div class="list-group-item list-group-item-action d-flex justify-content-between align-items-center">
                    <div>
                        <h5 class="mb-1">${template.name}</h5>
                        <p class="mb-1">${template.symbol} (${template.exchange}) - ${template.action} ${template.quantity} @ ${template.order_type}</p>
                    </div>
                    <div>
                        <button class="btn btn-sm btn-outline-primary load-template" data-index="${index}">Load</button>
                        <button class="btn btn-sm btn-outline-danger delete-template" data-index="${index}">Delete</button>
                    </div>
                </div>
            `;
        });
        html += '</div>';
        
        $('#savedTemplates').html(html);
        
        // Add event listeners for template buttons
        $('.load-template').click(function() {
            const index = $(this).data('index');
            loadTemplate(index);
        });
        
        $('.delete-template').click(function() {
            const index = $(this).data('index');
            deleteTemplate(index);
        });
    }
    
    // Load a template into the form
    function loadTemplate(index) {
        const templates = JSON.parse(localStorage.getItem('webhook_templates') || '[]');
        const template = templates[index];
        
        $('#action').val(template.action);
        $('#symbol').val(template.symbol);
        $('#symbol_token').val(template.symbol_token);
        $('#exchange').val(template.exchange);
        $('#product_type').val(template.product_type);
        $('#order_type').val(template.order_type);
        $('#quantity').val(template.quantity);
        $('#price').val(template.price);
        $('#take_profit').val(template.take_profit);
        $('#stop_loss').val(template.stop_loss);
        $('#trigger_price').val(template.trigger_price);
        
        // Update the display of conditional fields
        $('#order_type').trigger('change');
        
        // Generate the JSON
        $('#webhookForm').trigger('submit');
    }
    
    // Delete a template
    function deleteTemplate(index) {
        if (confirm('Are you sure you want to delete this template?')) {
            const templates = JSON.parse(localStorage.getItem('webhook_templates') || '[]');
            templates.splice(index, 1);
            localStorage.setItem('webhook_templates', JSON.stringify(templates));
            loadSavedTemplates();
        }
    }
    
    // Check if there's a template from symbol search
    const webhookTemplate = localStorage.getItem('webhook_template');
    if (webhookTemplate) {
        try {
            const template = JSON.parse(webhookTemplate);
            $('#action').val(template.action);
            $('#symbol').val(template.symbol);
            $('#symbol_token').val(template.symbol_token);
            $('#exchange').val(template.exchange);
            $('#product_type').val(template.product_type);
            $('#order_type').val(template.order_type);
            $('#quantity').val(template.quantity);
            if (template.price) $('#price').val(template.price);
            
            // Generate the JSON
            $('#webhookForm').trigger('submit');
            
            // Clear the temporary template
            localStorage.removeItem('webhook_template');
        } catch (e) {
            console.error('Error loading webhook template:', e);
        }
    }
    
    // Load saved templates on page load
    loadSavedTemplates();
});

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