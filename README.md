## About

This is an addon for [qToggleServer](https://github.com/qtoggle/qtoggleserver).

It provides ports that are backed by configurable HTTP requests.


## Install

Install using pip:

    pip install qtoggleserver-generic-http


## Usage

##### `qtoggleserver.conf:`
``` javascript
...
ports = [
    ...
    {
        driver = "qtoggleserver.generichttp.GenericHTTPClient"
        name = "myperipheral"  # a name of your choice
        read = {
            url = "https://api.example.com/myresource"
            method = GET  # default
            query = {
                name1 = "value1"
                ...
            }
            headers = {
                name2 = "value2"
                ...
            }
            cookies = {
                name3 = "value3"
                ...
            }
            request_body = {  # JSON or custom body string content
                "name4": "value4"
            }
        }
        write = {
            url = "https://api.example.com/myresource"  # inherited from read, if unspecified
            method = POST  # default
            query = {
                name1 = "value1"
                ...
            }
            headers = {
                name2 = "value2"
                ...
            }
            cookies = {
                name3 = "value3"
                ...
            }
            request_body = {  # JSON or custom body string content
                "name4": "value4"
            }
        }
        auth = {
            type = basic  # none (default) or basic
            username = "your_username"
            password = "your_password"
        }
        ignore_response_code = true  # see below, defaults to false
        ignore_invalid_cert = true   # whether to ignore TLS cerfificate issues or not, defaults to false
        timeout = 10                 # default request timeout is 10 seconds
        ports = {
            "port_id1" = {
                type = boolean                # boolean or number
                writable = true               # defaults to false
                read = {
                    json_path = "/path/to/field"  # RFC6901 JSON pointer to port value, inside response body
                    body_regex = "myvalue=(\d+)"  # regular expression inside body for port value lookup
                    true_value = 1                # value or list of values that are true (for boolean ports)
                }
                write = {
                    ...  # overrides to common write details above
                }
            },
            ...
        }
    }
    ...
]
...
```

### Placeholders

Placeholders are based on the [Jinja2](https://jinja.palletsprojects.com/) template rendering engine.

Any of the following fields may be given as templates containing _placeholders_:
 * `url`
 * `query`
 * `headers`
 * `cookies`
 * `request_body`

The following context variables are recognized when replacing placeholders:
 * `value` - the current port value
 * `new_value` - the new port value, available only when writing a value to port
 * `port` - the port itself
 * `attrs` - a dictionary with current port's attributes
 
Complex data structures containing lists or dictionaries will be parsed recursively and placeholders will be replaced
in each element or key.

For example, following request body will send the new value in a dictionary field called `"value"`:

    request_body = {
        "value": "{{new_value}}"
    }

All builtin Python functions are available to be used inside the placeholder expression. For more details, see the
[Jinja2 Template Designer Docs](https://jinja.palletsprojects.com/en/2.11.x/templates/).

Template strings containing placeholders must be enclosed in quotes. However, their final value will not be surrounded
by quotes unless it's a string itself.

If you really want quotes around your placeholder's real value, you can simply ensure that the final value is a string,
by passing it to the builtin Python function `str` (e.g. `{{str(new_value)}}`).

### Port Value Readings

Port values are read using the response to an HTTP request (one request for all defined ports). Intermediate
_raw values_ are determined from the response and are coerced to the data types of the respective ports.

A raw value is determined as follows:
 * if `ignore_response_code` is `false` (default) and status code is >= 400, the raw value is set to `false`, regardless
of the response body
 * otherwise, if `json_path` is specified, response body is interpreted as JSON and the raw value is looked up using
given JSON path (RFC6901 JSON pointer); if the lookup does not go well (for any reason), the raw value is _undefined_
 * otherwise, if `body_regex` is specified, a regex match is attempted on the entire body content; the first group (or
the entire match, if no group is given) is used to determine the raw value; on unsuccessful match, the raw value is
_undefined_
 * otherwise, the raw value is set to `true` if status code is < 300 and to `false` otherwise (3xx status codes will be
used internally by the HTTP client)

Now given a raw value, the actual port value is determined as follows:
 * if the raw value is _undefined_, the port value becomes _undefined_
 * for a `boolean` port, the value is `true` if the raw value is equal to `true_value` (or one of its items if
`true_value` is a list) and `false` otherwise
 * for a `number` port, the raw value is transformed to a number, unless it already is a number, as follows:
     * `true` is `1` and `false` is `0`
     * strings are converted to numbers, after being stripped of leading and trailing whitespace
     * any other raw value type will result in an _undefined_ port value

### Port Value Writings

Writing port values is done using an HTTP request whose response is ignored (but awaited, up to the given `timeout`).

As opposed to port readings, there will be one separate request for each port whose value changes.

### A Few Remarks

If the request body is not given as a string, it is assumed to be JSON and transmitted as such, including the
corresponding `Content-Type` header set to `application/json`.

