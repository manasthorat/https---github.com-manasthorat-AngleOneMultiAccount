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