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